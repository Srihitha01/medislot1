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
    result = ssm.get_parameter(Name=name, WithDecryption=with_decryption)
    return result["Parameter"]["Value"]


def check_identifier(value, label):
    if not IDENTIFIER_RE.fullmatch(value or ""):
        raise ValueError(f"{label} must be 1-32 letters, digits or underscores")
    return value


def _schema_statements():
    schema_path = Path(__file__).with_name("schema.sql")
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file is not packaged: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    # This schema contains ordinary CREATE TABLE statements and no stored procedures.
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def lambda_handler(event, context):
    environment = os.environ["ENVIRONMENT"]
    db_name = check_identifier(os.environ.get("DB_NAME", "medislot"), "DB_NAME")
    app_user = check_identifier(os.environ["DB_USER"], "DB_USER")
    master_user = os.environ.get("DB_MASTER_USERNAME", "medislotadmin")
    host = get_parameter(os.environ.get("DB_HOST_PARAMETER", f"/medislot/{environment}/db/host"))
    port = int(get_parameter(os.environ.get("DB_PORT_PARAMETER", f"/medislot/{environment}/db/port")))
    master_password = get_parameter(os.environ.get("DB_MASTER_PASSWORD_PARAMETER", f"/medislot/{environment}/db/password"), True)
    app_password = get_parameter(os.environ.get("DB_PASSWORD_PARAMETER", f"/medislot/{environment}/db/app-password"), True)

    connection = pymysql.connect(host=host, user=master_user, password=master_password,
        database=db_name, port=port, connect_timeout=10, read_timeout=30,
        write_timeout=30, autocommit=False, charset="utf8mb4")
    try:
        with connection.cursor() as cursor:
            for statement in _schema_statements():
                cursor.execute(statement)
            try:
                cursor.execute(f"CREATE USER IF NOT EXISTS '{app_user}'@'%' IDENTIFIED WITH mysql_native_password BY %s", (app_password,))
                cursor.execute(f"ALTER USER '{app_user}'@'%' IDENTIFIED WITH mysql_native_password BY %s", (app_password,))
            except pymysql.MySQLError:
                cursor.execute(f"CREATE USER IF NOT EXISTS '{app_user}'@'%' IDENTIFIED BY %s", (app_password,))
                cursor.execute(f"ALTER USER '{app_user}'@'%' IDENTIFIED BY %s", (app_password,))
            cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{db_name}`.* TO '{app_user}'@'%'")
            cursor.execute("FLUSH PRIVILEGES")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    logger.info(json.dumps({"event": "schema_initialized", "database": db_name, "app_user": app_user}))
    return {"statusCode": 200, "body": json.dumps({"status": "ok", "message": "UUID schema initialized", "database": db_name, "appUser": app_user})}
