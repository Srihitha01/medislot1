
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


def get_parameter(name, decrypt=False):
    response = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt,
    )
    return response["Parameter"]["Value"]


def lambda_handler(event, context):
    event = event or {}

    if event.get("action") not in (None, "initialize"):
        return {
            "statusCode": 400,
            "body": json.dumps({
                "status": "error",
                "message": "Unsupported action",
            }),
        }

    connection = None

    try:
        environment = os.environ["ENVIRONMENT"]
        database = os.environ.get("DB_NAME", "medislot")
        app_user = os.environ.get(
            "DB_APP_USERNAME",
            os.environ.get("DB_USER", "medislot_app"),
        )

        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
            raise ValueError("Invalid database name")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", app_user):
            raise ValueError("Invalid application database username")

        host = get_parameter(
            os.environ.get(
                "DB_HOST_PARAMETER",
                f"/medislot/{environment}/db/host",
            )
        )
        port = int(get_parameter(
            os.environ.get(
                "DB_PORT_PARAMETER",
                f"/medislot/{environment}/db/port",
            )
        ))

        master_user = os.environ.get(
            "DB_MASTER_USERNAME", "medislotadmin"
        )
        master_password = get_parameter(
            os.environ.get(
                "DB_MASTER_PASSWORD_PARAMETER",
                f"/medislot/{environment}/db/password",
            ),
            decrypt=True,
        )
        app_password = get_parameter(
            os.environ.get(
                "DB_APP_PASSWORD_PARAMETER",
                f"/medislot/{environment}/db/app-password",
            ),
            decrypt=True,
        )

        schema_path = Path(__file__).with_name("schema.sql")
        if not schema_path.is_file():
            raise FileNotFoundError(
                "schema.sql is missing from the Lambda package"
            )

        statements = [
            statement.strip()
            for statement in schema_path.read_text(
                encoding="utf-8"
            ).split(";")
            if statement.strip()
        ]
        if not statements:
            raise ValueError("schema.sql contains no SQL statements")

        connection = pymysql.connect(
            host=host,
            port=port,
            user=master_user,
            password=master_password,
            database=database,
            connect_timeout=10,
            read_timeout=20,
            write_timeout=20,
            autocommit=True,
        )

        with connection.cursor() as cursor:
            # Initialize existing tables without deleting data.
            for statement in statements:
                cursor.execute(statement)

            # Escape % as %% because PyMySQL performs %-style formatting.
            cursor.execute(
                f"CREATE USER IF NOT EXISTS '{app_user}'@'%%' "
                "IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute(
                f"ALTER USER '{app_user}'@'%%' IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON `{database}`.* TO '{app_user}'@'%%'"
            )

        # Verify application credentials using a real database connection.
        app_connection = pymysql.connect(
            host=host,
            port=port,
            user=app_user,
            password=app_password,
            database=database,
            connect_timeout=10,
            read_timeout=10,
        )
        try:
            with app_connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        finally:
            app_connection.close()

        logger.info(json.dumps({
            "event": "database_schema_and_app_user_verified",
            "environment": environment,
            "database": database,
            "applicationUser": app_user,
            "statementsExecuted": len(statements),
            "requestId": getattr(context, "aws_request_id", None),
        }))

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "environment": environment,
                "database": database,
                "application_user_verified": True,
                "statements_executed": len(statements),
            }),
        }

    except Exception:
        logger.exception(json.dumps({
            "event": "database_schema_initialization_failed",
            "requestId": getattr(context, "aws_request_id", None),
        }))
        raise

    finally:
        if connection is not None:
            connection.close()
