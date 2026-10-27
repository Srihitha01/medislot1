"""Initialize the MediSlot MySQL schema during deployment.

The workflow copies database/schema.sql beside this module before `sam build`.
The Lambda retrieves its connection settings from SSM Parameter Store and runs
only the version-controlled CREATE TABLE statements in that file.
"""
import json
import logging
import os
from pathlib import Path

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ssm = boto3.client("ssm")


def _parameter(name: str, decrypt: bool = False) -> str:
    response = _ssm.get_parameter(Name=name, WithDecryption=decrypt)
    return response["Parameter"]["Value"]


def lambda_handler(event, context):
    event = event or {}
    if event.get("action") not in (None, "initialize"):
        return {"statusCode": 400, "body": json.dumps({"status": "error", "message": "Unsupported action"})}

    db_name = os.environ.get("DB_NAME", "medislot")
    host = _parameter(os.environ["DB_HOST_PARAMETER"])
    port = int(_parameter(os.environ["DB_PORT_PARAMETER"]))
    password = _parameter(os.environ["DB_MASTER_PASSWORD_PARAMETER"], decrypt=True)

    schema_path = Path(__file__).with_name("schema.sql")
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not packaged with Lambda: {schema_path}")

    # This schema is DDL only (CREATE TABLE IF NOT EXISTS). Execute statements
    # individually because MySQL drivers do not consistently support multi-statements.
    sql_text = schema_path.read_text(encoding="utf-8")
    statements = [part.strip() for part in sql_text.split(";") if part.strip()]

    connection = pymysql.connect(
        host=host,
        port=port,
        user=os.environ.get("DB_MASTER_USERNAME", "medislotadmin"),
        password=password,
        database=db_name,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        logger.info("Schema initialization succeeded; statements_executed=%d", len(statements))
    finally:
        connection.close()

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "success", "statements_executed": len(statements)}),
    }
