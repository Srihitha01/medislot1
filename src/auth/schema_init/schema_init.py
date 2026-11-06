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

_STATEMENT_START = re.compile(
    r"(?im)^(?=\s*(?:CREATE\s+TABLE|INSERT\s+INTO)\b)"
)


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

    # Drop whole-line "--" comments so a comment never becomes (part of)
    # a statement and a stray ';' inside a comment cannot split one.
    content = "\n".join(
        line
        for line in content.splitlines()
        if not line.lstrip().startswith("--")
    )

    statements = []

    for chunk in content.split(";"):
        # Defensive: if a ';' is ever missing between two statements,
        # still treat each line-leading CREATE TABLE / INSERT INTO as
        # the start of its own statement.
        for statement in _STATEMENT_START.split(chunk):
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
    Connect to MediSlot MySQL as the RDS master user.

    RDS only creates the master user (medislotadmin). The application
    user (medislot_app) does not exist until _ensure_app_user() creates
    it, so the schema initializer must authenticate as the master user.
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

    master_password_parameter = os.environ.get(
        "DB_MASTER_PASSWORD_PARAMETER",
        f"/medislot/{environment}/db/password"
    )

    master_user = os.environ.get(
        "DB_MASTER_USERNAME",
        "medislotadmin"
    )

    host = get_parameter(host_parameter)

    port = int(get_parameter(port_parameter))

    master_password = get_parameter(
        master_password_parameter,
        decrypt=True
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=master_user,
        password=master_password,
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False
    )


_USER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_DB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _ensure_app_user(cursor, database):
    """
    Create (or refresh) the application MySQL user and grant it DML
    access to the MediSlot database. Idempotent.
    """
    environment = os.environ["ENVIRONMENT"]

    app_user = (
        os.environ.get("DB_APP_USERNAME")
        or os.environ.get("DB_USER")
        or "medislot_app"
    )

    if not _USER_NAME_PATTERN.match(app_user):
        raise ValueError("Invalid application database username")

    if not _DB_NAME_PATTERN.match(database):
        raise ValueError("Invalid database name")

    app_password_parameter = (
        os.environ.get("DB_APP_PASSWORD_PARAMETER")
        or os.environ.get("DB_PASSWORD_PARAMETER")
        or f"/medislot/{environment}/db/app-password"
    )

    app_password = get_parameter(
        app_password_parameter,
        decrypt=True
    )

    # '%%' is a literal '%' after PyMySQL parameter interpolation.
    steps = (
        (
            "create app user",
            "CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s",
            (app_user, app_password)
        ),
        (
            # Keep the MySQL password in sync with the SSM parameter.
            "set app user password",
            "ALTER USER %s@'%%' IDENTIFIED BY %s",
            (app_user, app_password)
        ),
        (
            "grant app user privileges",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{database}`.* "
            "TO %s@'%%'",
            (app_user,)
        ),
    )

    for label, sql, params in steps:
        try:
            cursor.execute(sql, params)
        except Exception as exc:
            logger.error("App user step failed: %s", label)
            try:
                exc.failed_step = label
            except Exception:
                pass
            raise


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
    for index, statement in enumerate(_schema_statements(), start=1):
        try:
            cursor.execute(statement)
        except Exception as exc:
            preview = " ".join(statement.split())[:80]
            logger.error(
                "Schema statement %d failed: %s", index, preview
            )
            try:
                exc.failed_step = f"schema statement {index}: {preview}"
            except Exception:
                pass
            raise


# ---------------------------------------------------------
# NON-DESTRUCTIVE BLACKOUT SCHEMA MIGRATION
# ---------------------------------------------------------

def _blackout_schema_columns(cursor, database):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'blackout_dates'
        """,
        (database,)
    )
    return {str(row["COLUMN_NAME"] if isinstance(row, dict) else row[0])
            for row in cursor.fetchall()}


def _blackout_schema_indexes(cursor, database):
    cursor.execute(
        """
        SELECT INDEX_NAME, NON_UNIQUE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'blackout_dates'
        """,
        (database,)
    )
    result = {}
    for row in cursor.fetchall():
        if isinstance(row, dict):
            name, non_unique = row["INDEX_NAME"], row["NON_UNIQUE"]
        else:
            name, non_unique = row[0], row[1]
        result[str(name)] = int(non_unique)
    return result


def _migrate_blackout_dates(cursor, database):
    """
    Safely upgrade an existing date-only blackout_dates table.

    Existing records remain full-day blackouts. Remove the old
    doctor/date unique constraint to permit multiple timed windows.
    Each DDL operation is guarded by information_schema checks,
    so subsequent initialize invocations are safe.
    """
    columns = _blackout_schema_columns(cursor, database)
    if not columns:
        raise RuntimeError("blackout_dates table missing after schema creation")

    additions = (
        ("is_full_day", "ALTER TABLE blackout_dates "
         "ADD COLUMN is_full_day TINYINT(1) NOT NULL DEFAULT 1 AFTER reason"),
        ("start_time", "ALTER TABLE blackout_dates "
         "ADD COLUMN start_time TIME NULL AFTER is_full_day"),
        ("end_time", "ALTER TABLE blackout_dates "
         "ADD COLUMN end_time TIME NULL AFTER start_time"),
    )
    for column, statement in additions:
        if column not in columns:
            logger.info("Adding blackout_dates.%s", column)
            cursor.execute(statement)
            columns.add(column)

    # Create the replacement index BEFORE dropping the old unique index.
    # MySQL requires an index beginning with doctor_user_id to enforce
    # fk_blackout_doctor. Dropping the only qualifying index raises 1553.
    indexes = _blackout_schema_indexes(cursor, database)
    if "idx_blackout_doctor_date" not in indexes:
        logger.info("Adding blackout date lookup index before dropping uniqueness")
        cursor.execute(
            "ALTER TABLE blackout_dates "
            "ADD INDEX idx_blackout_doctor_date (doctor_user_id, blackout_date)"
        )

    indexes = _blackout_schema_indexes(cursor, database)
    if "uq_blackout_doctor_date" in indexes:
        logger.info("Removing obsolete blackout date uniqueness constraint")
        cursor.execute("ALTER TABLE blackout_dates DROP INDEX uq_blackout_doctor_date")

    logger.info("Timed blackout schema verified")


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

                _ensure_app_user(cursor, database)

                _create_schema(cursor)
                _migrate_blackout_dates(cursor, database)

                conn.commit()

                logger.info(
                    "Schema initialized successfully"
                )

                return response(
                    200,
                    {
                        "status": "ok",
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

            _ensure_app_user(cursor, database)

            _create_schema(cursor)

            conn.commit()

            logger.info(
                "Database reset completed"
            )

            return response(
                200,
                {
                    "status": "ok",
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
    ) as exc:
        if conn:
            conn.rollback()

        logger.exception(
            "Schema initialization failed"
        )

        error_code = (
            exc.args[0]
            if exc.args and isinstance(exc.args[0], int)
            else None
        )

        return response(
            500,
            {
                "message": (
                    "Schema initialization failed"
                ),
                "error_type": type(exc).__name__,
                "error_code": error_code,
                "failed_step": getattr(exc, "failed_step", None)
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