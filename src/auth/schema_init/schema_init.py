
import json
import logging
import os
import re
from pathlib import Path

import boto3
import pymysql

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Do not initialize AWS clients during module import.
_ssm = None


# ---------------------------------------------------------
# RESPONSE HELPER
# ---------------------------------------------------------

def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }


# ---------------------------------------------------------
# LAZY SSM CLIENT
# ---------------------------------------------------------

def get_ssm_client():
    """
    Create the SSM client only when required.

    This prevents boto3 from attempting to load AWS
    credentials during unittest module imports.
    """
    global _ssm

    if _ssm is None:
        _ssm = boto3.client(
            "ssm",
            region_name=AWS_REGION
        )

    return _ssm


def get_parameter(name, decrypt=False):
    """
    Retrieve a parameter from AWS Systems Manager.
    """
    return get_ssm_client().get_parameter(
        Name=name,
        WithDecryption=decrypt
    )["Parameter"]["Value"]


# ---------------------------------------------------------
# SCHEMA FILE HELPERS
# ---------------------------------------------------------

def _schema_path():
    return SCHEMA_PATH


def _schema_statements():
    """
    Read SQL statements from the packaged schema.sql file.
    """
    schema_file = _schema_path()

    if not schema_file.is_file():
        raise FileNotFoundError(
            f"Schema file not found: {schema_file}"
        )

    content = schema_file.read_text(encoding="utf-8")

    statements = []

    for statement in content.split(";"):
        statement = statement.strip()

        if statement:
            statements.append(statement)

    return statements


def _table_names():
    """
    Return table names in schema creation order.
    """
    names = []

    for statement in _schema_statements():
        match = re.match(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?([A-Za-z0-9_]+)`?",
            statement,
            re.IGNORECASE
        )

        if match:
            names.append(match.group(1))

    return names


# ---------------------------------------------------------
# DATABASE SCHEMA DETECTION
# ---------------------------------------------------------

def _user_id_type(cursor, database):
    """
    Inspect the existing users.user_id column type.
    """
    cursor.execute(
        """
        SELECT DATA_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'user_id'
        LIMIT 1
        """,
        (database,)
    )

    row = cursor.fetchone()

    if not row:
        return None

    if isinstance(row, dict):
        column_type = row.get("DATA_TYPE")
    else:
        column_type = row[0]

    if column_type is None:
        return None

    return str(column_type).lower()


def _uses_old_uuid_ids(cursor, database):
    """
    Detect the previous UUID-based users.user_id column.
    """
    column_type = _user_id_type(cursor, database)

    return column_type in (
        "char",
        "varchar",
        "binary",
        "varbinary"
    )


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def _connect():
    """
    Connect to MediSlot MySQL using SSM parameters.
    """
    environment = os.environ["ENVIRONMENT"]

    host_parameter = os.environ.get(
        "DB_HOST_PARAMETER",
        f"/medislot/{environment}/db/host"
    )

    port_parameter = os.environ.get(
        "DB_PORT_PARAMETER",
        f"/medislot/{environment}/db/port"
    )

    password_parameter = os.environ.get(
        "DB_PASSWORD_PARAMETER",
        f"/medislot/{environment}/db/app-password"
    )

    host = get_parameter(host_parameter)

    port = int(get_parameter(port_parameter))

    password = get_parameter(
        password_parameter,
        decrypt=True
    )

    db_user = os.environ.get("DB_USER")

    if not db_user:
        db_user_parameter = os.environ.get(
            "DB_USER_PARAMETER"
        )

        if not db_user_parameter:
            raise ValueError(
                "DB_USER or DB_USER_PARAMETER must be configured"
            )

        db_user = get_parameter(db_user_parameter)

    return pymysql.connect(
        host=host,
        port=port,
        user=db_user,
        password=password,
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False
    )


# ---------------------------------------------------------
# SCHEMA OPERATIONS
# ---------------------------------------------------------

def _drop_schema(cursor):
    """
    Drop schema tables in reverse creation order.

    WARNING: This permanently deletes data and is used
    only by the explicitly confirmed reset action.
    """
    for table_name in reversed(_table_names()):
        cursor.execute(
            f"DROP TABLE IF EXISTS `{table_name}`"
        )


def _create_schema(cursor):
    """
    Execute the SQL schema without deleting existing data.
    """
    for statement in _schema_statements():
        cursor.execute(statement)


# ---------------------------------------------------------
# MAIN LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    """
    Supported actions:

    initialize:
        Initialize missing schema tables.
        Refuse existing UUID-based user IDs.

    reset:
        Destructive reset, restricted to dev/test and
        requiring explicit confirmation.

    This function does not migrate existing UUID records.
    """
    event = event or {}

    action = event.get("action", "initialize")

    if action not in ("initialize", "reset"):
        raise ValueError("Unknown action")

    environment = os.environ.get(
        "ENVIRONMENT",
        ""
    ).lower()

    # -----------------------------------------------------
    # VALIDATE RESET REQUEST
    # -----------------------------------------------------

    if action == "reset":
        if event.get("confirm") != "DELETE_ALL_DATA":
            raise ValueError(
                "Reset requires confirm=DELETE_ALL_DATA"
            )

        if environment not in ("dev", "test"):
            raise ValueError(
                "Database reset is allowed only in dev or test"
            )

    conn = None

    try:
        conn = _connect()

        database = os.environ.get(
            "DB_NAME",
            "medislot"
        )

        with conn.cursor() as cursor:

            # -------------------------------------------------
            # INITIALIZE
            # -------------------------------------------------

            if action == "initialize":

                if _uses_old_uuid_ids(cursor, database):
                    conn.rollback()

                    logger.warning(
                        "Old UUID-based schema detected"
                    )

                    return response(
                        409,
                        {
                            "message": (
                                "Existing UUID-based user IDs "
                                "were detected. Back up and "
                                "migrate existing data before "
                                "initializing the numeric-ID "
                                "schema. Do not reset a database "
                                "containing records you need "
                                "to preserve."
                            )
                        }
                    )

                _create_schema(cursor)

                conn.commit()

                logger.info(
                    "Schema initialized successfully"
                )

                return response(
                    200,
                    {
                        "message": (
                            "Schema initialized successfully"
                        )
                    }
                )

            # -------------------------------------------------
            # RESET - DESTRUCTIVE
            # -------------------------------------------------

            logger.warning(
                "Explicit database reset requested"
            )

            _drop_schema(cursor)

            _create_schema(cursor)

            conn.commit()

            logger.info(
                "Database reset completed"
            )

            return response(
                200,
                {
                    "message": (
                        "Database reset and schema created"
                    )
                }
            )

    # ---------------------------------------------------------
    # ERROR HANDLING
    # ---------------------------------------------------------

    except (
        BotoCoreError,
        ClientError,
        pymysql.MySQLError
    ):
        if conn:
            conn.rollback()

        logger.exception(
            "Schema initialization failed"
        )

        return response(
            500,
            {
                "message": (
                    "Schema initialization failed"
                )
            }
        )

    except Exception:
        if conn:
            conn.rollback()

        logger.exception(
            "Unexpected schema initialization error"
        )

        raise

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                logger.warning(
                    "Failed to close database connection"
                )
