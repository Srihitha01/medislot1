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
    response = ssm.get_parameter(Name=name, WithDecryption=decrypt)
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
    app_connection = None

    try:
        environment = os.environ["ENVIRONMENT"]
        database = os.environ.get("DB_NAME", "medislot")
        app_user = os.environ.get("DB_APP_USERNAME", os.environ.get("DB_USER", "medislot_app"))

        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
            raise ValueError("Invalid database name")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", app_user):
            raise ValueError("Invalid application database username")

        host = get_parameter(
            os.environ.get("DB_HOST_PARAMETER", f"/medislot/{environment}/db/host")
        )
        port = int(get_parameter(
            os.environ.get("DB_PORT_PARAMETER", f"/medislot/{environment}/db/port")
        ))

        master_user = os.environ.get("DB_MASTER_USERNAME", "medislotadmin")
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

        # This file is included in the existing schema-init Lambda package by SAM.
        schema_path = Path(__file__).with_name("schema.sql")
        if not schema_path.is_file():
            raise FileNotFoundError(
                f"Required schema file is missing from Lambda package: {schema_path.name}"
            )

        # The project schema contains ordinary SQL statements separated by semicolons.
        statements = [
            statement.strip()
            for statement in schema_path.read_text(encoding="utf-8").split(";")
            if statement.strip()
        ]
        if not statements:
            raise ValueError("schema.sql contains no SQL statements")

        # Connect to RDS using the RDS master account to initialize the schema.
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
            for statement in statements:
                cursor.execute(statement)

            # MySQL 8 / RDS requires the account to exist before GRANT.
            # Keep the wildcard host as '%' consistently in all three statements.
            # Use %% only in statements that have PyMySQL %s placeholders, because
            # PyMySQL interpolates the query string when parameters are supplied.
            cursor.execute(
                f"CREATE USER IF NOT EXISTS '{app_user}'@'%%' IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute(
                f"ALTER USER '{app_user}'@'%%' IDENTIFIED BY %s",
                (app_password,),
            )
            # No bound parameters here, so use a literal single '%' host.
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{database}`.* "
                f"TO '{app_user}'@'%'"
            )

        # Verify the application credentials can connect to the same RDS database.
        app_connection = pymysql.connect(
            host=host,
            port=port,
            user=app_user,
            password=app_password,
            database=database,
            connect_timeout=10,
            read_timeout=10,
            write_timeout=10,
        )
        with app_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        result = {
            "status": "success",
            "environment": environment,
            "database": database,
            "application_user_verified": True,
            "statements_executed": len(statements),
        }
        logger.info(json.dumps({
            "event": "database_schema_and_app_user_verified",
            "environment": environment,
            "database": database,
            "applicationUser": app_user,
            "statementsExecuted": len(statements),
            "requestId": getattr(context, "aws_request_id", None),
        }))

        return {"statusCode": 200, "body": json.dumps(result)}

    except Exception:
        logger.exception(json.dumps({
            "event": "database_schema_initialization_failed",
            "requestId": getattr(context, "aws_request_id", None),
        }))
        raise

    finally:
        if app_connection is not None:
            app_connection.close()
        if connection is not None:
            connection.close()
