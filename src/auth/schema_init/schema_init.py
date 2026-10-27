import json
import logging
import os
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

        username = os.environ.get(
            "DB_MASTER_USERNAME",
            "medislotadmin",
        )

        password_parameter = os.environ.get(
            "DB_MASTER_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/password",
        )

        password = get_parameter(
            password_parameter,
            decrypt=True,
        )

        schema_path = Path(__file__).with_name("schema.sql")

        if not schema_path.is_file():
            raise FileNotFoundError(
                "schema.sql is missing from the Lambda package"
            )

        sql_text = schema_path.read_text(encoding="utf-8")
        statements = [
            statement.strip()
            for statement in sql_text.split(";")
            if statement.strip()
        ]

        if not statements:
            raise ValueError("schema.sql contains no SQL statements")

        connection = pymysql.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=database,
            connect_timeout=10,
            read_timeout=20,
            write_timeout=20,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

        logger.info(json.dumps({
            "event": "database_schema_initialized",
            "environment": environment,
            "database": database,
            "statementsExecuted": len(statements),
            "requestId": getattr(
                context, "aws_request_id", None
            ),
        }))

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "environment": environment,
                "database": database,
                "statements_executed": len(statements),
            }),
        }

    except Exception:
        logger.exception(json.dumps({
            "event": "database_schema_initialization_failed",
            "requestId": getattr(
                context, "aws_request_id", None
            ),
        }))
        raise

    finally:
        if connection is not None:
            connection.close()