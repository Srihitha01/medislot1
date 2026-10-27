import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid

import bcrypt
import boto3
import jwt
import pymysql
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

TOKEN_TABLE_NAME = os.environ["AUTH_TOKENS_TABLE"]
token_table = dynamodb.Table(TOKEN_TABLE_NAME)

ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))
JWT_ALGORITHM = "HS256"


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def get_parameter(name, decrypt=False, version=None):
    parameter_name = f"{name}:{version}" if version else name
    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=decrypt,
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    environment = os.environ["ENVIRONMENT"]
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
    password = get_parameter(
        os.environ.get(
            "DB_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/app-password",
        ),
        decrypt=True,
        version=os.environ.get("DB_PASSWORD_PARAMETER_VERSION"),
    )

    return pymysql.connect(
        host=host,
        user=os.environ["DB_USER"],
        password=password,
        database=os.environ.get("DB_NAME", "medislot"),
        port=port,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_jwt_secret():
    environment = os.environ["ENVIRONMENT"]
    return get_parameter(
        os.environ.get(
            "JWT_SECRET_PARAMETER",
            f"/medislot/{environment}/auth/jwt-secret",
        ),
        decrypt=True,
        version=os.environ.get("JWT_SECRET_PARAMETER_VERSION"),
    )


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def requested_role(event):
    path = (event.get("path") or event.get("rawPath") or "").rstrip("/")
    if path.endswith("/auth/doctors/login"):
        return "doctor"
    if path.endswith("/auth/patients/login"):
        return "patient"
    return None


def lambda_handler(event, context):
    correlation_id = getattr(context, "aws_request_id", str(uuid.uuid4()))

    try:
        raw_body = event.get("body") or "{}"
        data = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        if not isinstance(data, dict):
            return response(400, {"message": "Invalid request body"})

        email = data.get("email")
        password = data.get("password")
        role = requested_role(event)

        if not isinstance(email, str) or not isinstance(password, str):
            return response(400, {"message": "Email and password are required"})

        email = email.strip().lower()
        if not email or len(email) > 254 or not re.fullmatch(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email
        ):
            return response(400, {"message": "Invalid email address"})

        if role not in {"doctor", "patient"}:
            return response(400, {"message": "Invalid login endpoint"})

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, email, password_hash, role
                    FROM users
                    WHERE email = %s
                    LIMIT 1
                    """,
                    (email,),
                )
                user = cursor.fetchone()
        finally:
            connection.close()

        # Use the same generic response for unknown emails and wrong passwords.
        if not user or user.get("role") != role:
            return response(401, {"message": "Invalid email or password"})

        stored_hash = user["password_hash"]
        if not bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash.encode("utf-8"),
        ):
            return response(401, {"message": "Invalid email or password"})

        now = int(time.time())
        jti = str(uuid.uuid4())
        access_expiry = now + ACCESS_TOKEN_MINUTES * 60
        refresh_expiry = now + REFRESH_TOKEN_DAYS * 24 * 60 * 60

        payload = {
            "sub": str(user["user_id"]),
            "role": role,
            "iat": now,
            "exp": access_expiry,
            "jti": jti,
        }
        access_token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

        # Only the hash of the opaque refresh token is stored.
        refresh_token = secrets.token_urlsafe(48)
        token_table.put_item(
            Item={
                "tokenId": hash_token(refresh_token),
                "tokenType": "REFRESH",
                "userId": str(user["user_id"]),
                "role": role,
                "expiresAt": refresh_expiry,
            },
            ConditionExpression="attribute_not_exists(tokenId)",
        )

        logger.info(json.dumps({
            "event": "login_succeeded",
            "correlation_id": correlation_id,
            "role": role,
        }))

        return response(200, {
            "accessToken": access_token,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_TOKEN_MINUTES * 60,
            "refreshToken": refresh_token,
            "refreshExpiresIn": REFRESH_TOKEN_DAYS * 24 * 60 * 60,
            "user": {
                "userId": str(user["user_id"]),
                "email": user["email"],
                "role": role,
            },
        })

    except (BotoCoreError, ClientError) as exc:
        logger.exception(json.dumps({
            "event": "login_dependency_error",
            "correlation_id": correlation_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:200],
        }))
        return response(500, {"message": "Login could not be completed"})
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {"message": "Invalid JSON request"})
    except Exception as exc:
        logger.exception(json.dumps({
            "event": "login_failed",
            "correlation_id": correlation_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:200],
        }))
        return response(500, {"message": "Login could not be completed"})