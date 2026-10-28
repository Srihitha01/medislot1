import json
import logging
import os
import re

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

# Only letters, digits and underscores are allowed because MySQL account and
# database names cannot be passed as bound parameters.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(60) NOT NULL,
    role ENUM('patient', 'doctor') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB
"""


def get_parameter(name, with_decryption=False):
    result = ssm.get_parameter(Name=name, WithDecryption=with_decryption)
    return result["Parameter"]["Value"]


def check_identifier(value, label):
    if not IDENTIFIER_RE.fullmatch(value or ""):
        raise ValueError(f"{label} must be 1-32 letters, digits or underscores")
    return value


def lambda_handler(event, context):
    environment = os.environ["ENVIRONMENT"]
    db_name = check_identifier(os.environ.get("DB_NAME", "medislot"), "DB_NAME")
    app_user = check_identifier(os.environ["DB_USER"], "DB_USER")
    master_user = os.environ.get("DB_MASTER_USERNAME", "medislotadmin")

    host = get_parameter(
        os.environ.get("DB_HOST_PARAMETER", f"/medislot/{environment}/db/host")
    )
    port = int(
        get_parameter(
            os.environ.get("DB_PORT_PARAMETER", f"/medislot/{environment}/db/port")
        )
    )
    master_password = get_parameter(
        os.environ.get(
            "DB_MASTER_PASSWORD_PARAMETER", f"/medislot/{environment}/db/password"
        ),
        with_decryption=True,
    )
    app_password = get_parameter(
        os.environ.get(
            "DB_PASSWORD_PARAMETER", f"/medislot/{environment}/db/app-password"
        ),
        with_decryption=True,
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
        autocommit=True,
    )

    try:
        with connection.cursor() as cursor:
            # Development only: invoke with {"recreate_users": true} to drop the
            # users table (and every user in it) before recreating it.
            if isinstance(event, dict) and event.get("recreate_users") is True:
                cursor.execute("DROP TABLE IF EXISTS users")
            cursor.execute(USERS_TABLE_SQL)

            # mysql_native_password works with PyMySQL without extra packages.
            # Newer MySQL versions may disable it, so fall back to the default.
            try:
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS '{app_user}'@'%%' "
                    "IDENTIFIED WITH mysql_native_password BY %s",
                    (app_password,),
                )
                cursor.execute(
                    f"ALTER USER '{app_user}'@'%%' "
                    "IDENTIFIED WITH mysql_native_password BY %s",
                    (app_password,),
                )
            except pymysql.MySQLError:
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS '{app_user}'@'%%' IDENTIFIED BY %s",
                    (app_password,),
                )
                cursor.execute(
                    f"ALTER USER '{app_user}'@'%%' IDENTIFIED BY %s",
                    (app_password,),
                )

            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{db_name}`.* "
                f"TO '{app_user}'@'%%'"
            )
            cursor.execute("FLUSH PRIVILEGES")
    finally:
        connection.close()

    logger.info(
        json.dumps({"event": "schema_initialized", "database": db_name, "app_user": app_user})
    )
    return {
        "statusCode": 200,
        "body": json.dumps(
            {"message": "Schema initialized", "database": db_name, "appUser": app_user}
        ),
    }