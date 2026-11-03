
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



def ensure_sequential_public_user_ids(connection):
    """Add public_user_id without changing existing UUID primary keys.

    Existing UUIDs remain the internal FK/JWT/DynamoDB identity. The numeric
    ID is an additional stable, auto-incrementing public identifier.
    Safe to run repeatedly. Run during a controlled deployment window so
    registrations are not occurring while a legacy table is being backfilled.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS column_count
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'public_user_id'
            """
        )
        if int(cursor.fetchone()[0]) == 0:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN public_user_id "
                "BIGINT UNSIGNED NULL"
            )

        # Fill only missing values; never renumber an ID already assigned.
        cursor.execute(
            "SELECT COALESCE(MAX(public_user_id), 0) AS max_id "
            "FROM users"
        )
        next_id = int(cursor.fetchone()[0]) + 1
        cursor.execute(
            "SELECT user_id FROM users "
            "WHERE public_user_id IS NULL ORDER BY created_at, user_id"
        )
        missing_users = cursor.fetchall()
        for row in missing_users:
            cursor.execute(
                "UPDATE users SET public_user_id = %s WHERE user_id = %s "
                "AND public_user_id IS NULL",
                (next_id, row[0])
            )
            next_id += 1

        cursor.execute(
            """
            SELECT COUNT(*) AS index_count
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND INDEX_NAME = 'uq_users_public_id'
            """
        )
        if int(cursor.fetchone()[0]) == 0:
            cursor.execute(
                "ALTER TABLE users ADD UNIQUE KEY uq_users_public_id "
                "(public_user_id)"
            )

        # AUTO_INCREMENT starts above the largest preserved/backfilled ID.
        cursor.execute(
            "ALTER TABLE users MODIFY COLUMN public_user_id "
            "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT"
        )


def lambda_handler(event, context):
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

            # Create all database tables
            for statement in _schema_statements():
                cursor.execute(statement)

            # Upgrade existing UUID-based installations without changing
            # primary keys or any existing foreign-key relationships.
            ensure_sequential_public_user_ids(connection)

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
            "message": "Schema initialized; sequential public user IDs verified",
            "database": db_name,
            "appUser": app_user
        })
    }
