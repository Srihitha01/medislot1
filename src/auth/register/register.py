
import json
import logging
import os
import re
import uuid

import boto3
import bcrypt
import pymysql
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "10"))
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

ssm = boto3.client("ssm")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def log_event(level, event_name, correlation_id, **fields):
    record = {
        "event": event_name,
        "correlation_id": correlation_id,
        **fields,
    }
    getattr(logger, level)(json.dumps(record))


def get_parameter(name, with_decryption=False):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=with_decryption,
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    environment = os.environ["ENVIRONMENT"]

    host_parameter = os.environ.get(
        "DB_HOST_PARAMETER",
        f"/medislot/{environment}/db/host",
    )
    port_parameter = os.environ.get(
        "DB_PORT_PARAMETER",
        f"/medislot/{environment}/db/port",
    )
    password_parameter = os.environ.get(
        "DB_PASSWORD_PARAMETER",
        f"/medislot/{environment}/db/app-password",
    )

    host = get_parameter(host_parameter)
    port = int(get_parameter(port_parameter))
    password = get_parameter(password_parameter, with_decryption=True)

    return pymysql.connect(
        host=host,
        user=os.environ["DB_USER"],
        password=password,
        database=os.environ.get("DB_NAME", "medislot"),
        port=port,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def parse_body(event):
    raw_body = event.get("body") or "{}"

    if isinstance(raw_body, dict):
        return raw_body

    if isinstance(raw_body, str):
        parsed = json.loads(raw_body)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Request body must be a JSON object")


def lambda_handler(event, context):
    correlation_id = (
        getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    )
    connection = None

    try:
        try:
            data = parse_body(event)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return response(400, {"message": "Invalid JSON request"})

        name = data.get("name")
        email = data.get("email")
        password = data.get("password")

        # The endpoint determines the role. Do not let the caller register
        # as a doctor by changing a request-body role field.
        request_path = (
            event.get("path")
            or event.get("rawPath")
            or ""
        ).rstrip("/")

        if request_path.endswith("/auth/doctors/register"):
            role = "doctor"
        elif request_path.endswith("/auth/patients/register"):
            role = "patient"
        else:
            # Supports local invocation and test events with an explicit role.
            role = data.get("role")

        if not all(
            isinstance(value, str)
            for value in (name, email, password, role)
        ):
            return response(
                400,
                {"message": "name, email and password are required"},
            )

        name = name.strip()
        email = email.strip().lower()
        role = role.strip().lower()

        if not name or len(name) > 120:
            return response(400, {"message": "Invalid name"})

        if len(email) > 254 or not EMAIL_RE.fullmatch(email):
            return response(400, {"message": "Invalid email address"})

        if role not in {"patient", "doctor"}:
            return response(400, {"message": "Invalid registration endpoint"})

        # MediSlot rule: minimum 10 characters, including at least one
        # letter and one digit. bcrypt only processes 72 input bytes.
        password_bytes = password.encode("utf-8")
        if (
            len(password) < PASSWORD_MIN_LENGTH
            or len(password_bytes) > 72
            or not re.search(r"[A-Za-z]", password)
            or not re.search(r"\d", password)
        ):
            return response(
                400,
                {
                    "message": (
                        "Password must be at least "
                        f"{PASSWORD_MIN_LENGTH} characters and include "
                        "at least one letter and one number; "
                        "maximum supported length is 72 UTF-8 bytes"
                    )
                },
            )

        password_hash = bcrypt.hashpw(
            password_bytes,
            bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
        ).decode("utf-8")

        connection = get_db_connection()

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users
                        (user_id, name, email, password_hash, role)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        name,
                        email,
                        password_hash,
                        role,
                    ),
                )
                connection.commit()

        except pymysql.err.IntegrityError as exc:
            connection.rollback()

            if exc.args and exc.args[0] == 1062:
                log_event(
                    "info",
                    "registration_duplicate_email",
                    correlation_id,
                    role=role,
                )
                return response(
                    409,
                    {"message": "An account with this email already exists"},
                )

            log_event(
                "warning",
                "registration_database_conflict",
                correlation_id,
                role=role,
            )
            return response(
                400,
                {"message": "Registration could not be completed"},
            )

        finally:
            connection.close()
            connection = None

        log_event(
            "info",
            "registration_succeeded",
            correlation_id,
            role=role,
        )

        return response(
            201,
            {
                "message": "Registration successful",
                "role": role,
            },
        )

    except (BotoCoreError, ClientError):
        log_event(
            "error",
            "registration_configuration_error",
            correlation_id,
        )
        return response(
            500,
            {
                "message": "Registration could not be completed",
                "correlationId": correlation_id,
            },
        )

    except Exception:
        log_event(
            "error",
            "registration_failed",
            correlation_id,
        )
        return response(
            500,
            {
                "message": "Registration could not be completed",
                "correlationId": correlation_id,
            },
        )

    finally:
        if connection is not None:
            connection.close()
