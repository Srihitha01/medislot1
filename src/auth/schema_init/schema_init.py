
import json
import logging
import os
import re
from pathlib import Path

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def get_parameter(name, with_decryption=False):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=with_decryption
    )
    return result["Parameter"]["Value"]


def check_identifier(value, label):
    if not IDENTIFIER_RE.fullmatch(value or ""):
        raise ValueError(
            f"{label} must be 1-32 letters, digits or underscores"
        )
    return value


def _schema_statements():
    schema_path = Path(__file__).with_name("schema.sql")

    if not schema_path.exists():
        raise FileNotFoundError(
            f"Schema file is not packaged: {schema_path}"
        )

    sql = schema_path.read_text(encoding="utf-8")

    return [
        statement.strip()
        for statement in sql.split(";")
        if statement.strip()
    ]


def _table_names():
    """Table names in the order the schema file creates them."""
    sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    # Only real statements: a line that starts with CREATE TABLE.
    # (Comment lines start with "--" and are skipped.)
    names = re.findall(
        r"^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.MULTILINE
    )

    for name in names:
        check_identifier(name, "table name")

    return names


def _uses_old_uuid_ids(cursor, db_name):
    """True when users.user_id is still the old text (UUID) column."""
    cursor.execute(
        "SELECT DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' "
        "AND COLUMN_NAME = 'user_id'",
        (db_name,)
    )
    row = cursor.fetchone()

    return bool(row) and str(row[0]).lower() in ("char", "varchar")


def _drop_all_tables(cursor):
    """Delete every MediSlot table. Development use only."""
    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

    for name in reversed(_table_names()):
        cursor.execute(f"DROP TABLE IF EXISTS `{name}`")

    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def _reset_allowed(environment, event):
    """A reset deletes all data, so it needs an explicit confirmation."""
    if environment not in ("dev", "test"):
        raise ValueError("reset is only allowed in the dev and test environments")

    if event.get("confirm") != "DELETE_ALL_DATA":
        raise ValueError('reset needs "confirm": "DELETE_ALL_DATA" in the event')


def lambda_handler(event, context):
    event = event or {}
    action = event.get("action", "initialize")

    if action not in ("initialize", "reset"):
        raise ValueError('action must be "initialize" or "reset"')

    environment = os.environ["ENVIRONMENT"]

    db_name = check_identifier(
        os.environ.get("DB_NAME", "medislot"),
        "DB_NAME"
    )

    app_user = check_identifier(
        os.environ["DB_USER"],
        "DB_USER"
    )

    master_user = os.environ.get(
        "DB_MASTER_USERNAME",
        "medislotadmin"
    )

    host = get_parameter(
        os.environ.get(
            "DB_HOST_PARAMETER",
            f"/medislot/{environment}/db/host"
        )
    )

    port = int(get_parameter(
        os.environ.get(
            "DB_PORT_PARAMETER",
            f"/medislot/{environment}/db/port"
        )
    ))

    master_password = get_parameter(
        os.environ.get(
            "DB_MASTER_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/password"
        ),
        True
    )

    app_password = get_parameter(
        os.environ.get(
            "DB_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/app-password"
        ),
        True
    )

    connection = pymysql.connect(
        host=host,
        user=master_user,
        password=master_password,
        database=db_name,
        port=port,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
        charset="utf8mb4"
    )

    try:
        with connection.cursor() as cursor:

            if action == "reset":
                _reset_allowed(environment, event)
                _drop_all_tables(cursor)

            elif _uses_old_uuid_ids(cursor, db_name):
                # CREATE TABLE IF NOT EXISTS cannot change existing columns.
                connection.rollback()
                logger.error("Old UUID tables found; reset is required")

                return {
                    "statusCode": 409,
                    "body": json.dumps({
                        "status": "error",
                        "message": (
                            "The database still has the old UUID user ids. "
                            "Run the schema-init Lambda once with "
                            '{"action": "reset", "confirm": "DELETE_ALL_DATA"} '
                            "(this deletes all data), then deploy again."
                        )
                    })
                }

            # Create all database tables
            for statement in _schema_statements():
                cursor.execute(statement)

            # Safely escape the application password
            escaped_password = connection.escape(app_password)

            try:
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS "
                    f"'{app_user}'@'%' "
                    f"IDENTIFIED BY {escaped_password}"
                )

            except pymysql.MySQLError:
                cursor.execute(
                    f"ALTER USER '{app_user}'@'%' "
                    f"IDENTIFIED BY {escaped_password}"
                )

            # Grant application permissions
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON `{db_name}`.* "
                f"TO '{app_user}'@'%'"
            )

            cursor.execute("FLUSH PRIVILEGES")

        connection.commit()

    except Exception:
        connection.rollback()
        logger.exception("Schema initialization failed")
        raise

    finally:
        connection.close()

    logger.info(json.dumps({
        "event": "schema_initialized",
        "database": db_name,
        "app_user": app_user
    }))

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "message": (
                "Database reset and schema created"
                if action == "reset"
                else "Schema initialized"
            ),
            "database": db_name,
            "appUser": app_user
        })
    }
