
import hashlib
import json
import logging
import os
import re
import secrets
import time

import bcrypt
import boto3
import jwt
import pymysql

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

tokens = ddb.Table(os.environ["AUTH_TOKENS_TABLE"])

ACCESS_MIN = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))

USER_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
MAX_UNSIGNED_BIGINT = 18446744073709551615


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": (
                "Content-Type,Authorization,Idempotency-Key,"
                "X-Correlation-Id"
            ),
            "Access-Control-Allow-Methods": (
                "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            )
        },
        "body": json.dumps(body)
    }


def param(name, decrypt=False):
    return ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )["Parameter"]["Value"]


def secret():
    environment = os.environ["ENVIRONMENT"]
    return param(
        os.environ.get(
            "JWT_SECRET_PARAMETER",
            f"/medislot/{environment}/auth/jwt-secret"
        ),
        True
    )


def db():
    environment = os.environ["ENVIRONMENT"]

    return pymysql.connect(
        host=param(os.environ.get(
            "DB_HOST_PARAMETER",
            f"/medislot/{environment}/db/host"
        )),
        port=int(param(os.environ.get(
            "DB_PORT_PARAMETER",
            f"/medislot/{environment}/db/port"
        ))),
        user=os.environ["DB_USER"],
        password=param(os.environ.get(
            "DB_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/app-password"
        ), True),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=10,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )


def hash_token(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_user_id(value):
    user_id = str(value)

    if not USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError("Invalid numeric user ID")

    numeric_id = int(user_id)

    if numeric_id > MAX_UNSIGNED_BIGINT:
        raise ValueError("User ID exceeds BIGINT UNSIGNED limit")

    return numeric_id


def save_session(
    user_id,
    role,
    access_jti,
    session_id,
    access_exp,
    refresh,
    refresh_exp
):
    user_id = validate_user_id(user_id)
    user_id_text = str(user_id)
    refresh_key = hash_token(refresh)

    # Save the refresh token first.
    tokens.put_item(
        Item={
            "tokenId": refresh_key,
            "tokenType": "REFRESH",
            "userId": user_id_text,
            "role": role,
            "sessionId": session_id,
            "expiresAt": refresh_exp
        }
    )

    try:
        # One active login session per numeric user ID.
        tokens.put_item(
            Item={
                "tokenId": f"ACTIVE#{user_id_text}",
                "tokenType": "ACTIVE_ACCESS",
                "userId": user_id_text,
                "jti": access_jti,
                "sessionId": session_id,
                "expiresAt": access_exp
            }
        )

    except Exception:
        try:
            tokens.delete_item(
                Key={"tokenId": refresh_key},
                ConditionExpression="sessionId = :sid",
                ExpressionAttributeValues={
                    ":sid": session_id
                }
            )
        except Exception:
            logger.exception(
                "Refresh-token rollback failed for user %s",
                user_id_text
            )
        raise


def _lambda_handler_impl(event, context):
    try:
        raw = event.get("body") or "{}"
        data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return response(400, {"message": "Invalid request body"})

        email = data.get("email")
        password = data.get("password")

        if not isinstance(email, str) or not isinstance(password, str):
            return response(400, {
                "message": "Email and password are required"
            })

        email = email.strip().lower()

        if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return response(400, {"message": "Invalid email address"})

        conn = db()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        user_id,
                        email,
                        password_hash,
                        role,
                        status
                    FROM users
                    WHERE email = %s
                    LIMIT 1
                    """,
                    (email,)
                )
                user = cur.fetchone()
        finally:
            conn.close()

        if not user or user["status"] != "active":
            return response(401, {
                "message": "Invalid email or password"
            })

        stored_hash = user["password_hash"]
        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        if not bcrypt.checkpw(password.encode("utf-8"), stored_hash):
            return response(401, {
                "message": "Invalid email or password"
            })

        # MySQL numeric identity.
        user_id = validate_user_id(user["user_id"])
        user_id_text = str(user_id)
        role = user["role"]

        now = int(time.time())
        access_jti = secrets.token_urlsafe(24)
        session_id = secrets.token_urlsafe(32)

        access_exp = now + ACCESS_MIN * 60
        refresh_exp = now + REFRESH_DAYS * 86400

        access = jwt.encode(
            {
                "sub": user_id_text,
                "role": role,
                "iat": now,
                "exp": access_exp,
                "jti": access_jti,
                "sid": session_id
            },
            secret(),
            algorithm="HS256"
        )

        refresh = secrets.token_urlsafe(48)

        save_session(
            user_id=user_id,
            role=role,
            access_jti=access_jti,
            session_id=session_id,
            access_exp=access_exp,
            refresh=refresh,
            refresh_exp=refresh_exp
        )

        logger.info(json.dumps({
            "event": "login_succeeded",
            "userId": user_id,
            "role": role
        }))

        return response(200, {
            "accessToken": access,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_MIN * 60,
            "refreshToken": refresh,
            "refreshExpiresIn": REFRESH_DAYS * 86400,
            "userId": user_id,
            "role": role
        })

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {"message": "Invalid JSON request"})

    except ValueError as exc:
        logger.warning("Login validation failed: %s", str(exc))
        return response(401, {"message": "Invalid user identity"})

    except (BotoCoreError, ClientError, pymysql.MySQLError):
        logger.exception("Login dependency failure")
        return response(500, {
            "message": "Login could not be completed"
        })

    except Exception:
        logger.exception("Login failure")
        return response(500, {
            "message": "Login could not be completed"
        })

def _log_http_response(event, context, result, started_at):
    """Emit one safe, structured response record for every HTTP invocation."""
    import time as _time
    status = int((result or {}).get("statusCode", 500))
    if 200 <= status < 300:
        level, outcome = "info", "success"
    elif status in (401, 403):
        level, outcome = "warning", "access_denied"
    elif status == 409:
        level, outcome = "info", "business_conflict"
    elif status == 404:
        level, outcome = "info", "resource_not_found"
    elif 400 <= status < 500:
        level, outcome = "warning", "client_rejected"
    else:
        level, outcome = "error", "server_failure"
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    record = {
        "event": "http_response",
        "service": 'auth-login',
        "method": event.get("httpMethod") or http.get("method"),
        "path": event.get("path") or event.get("rawPath"),
        "statusCode": status,
        "statusDescription": (
            "Request processed successfully" if 200 <= status < 300 else
            "Authentication is missing, invalid, expired, or inactive" if status == 401 else
            "Authenticated user is not permitted to perform this operation" if status == 403 else
            "Requested resource or route was not found" if status == 404 else
            "Request conflicts with the current resource or idempotency state" if status == 409 else
            "Request validation or method check was rejected" if 400 <= status < 500 else
            "Unexpected application or dependency failure"
        ),
        "outcome": outcome,
        "requestId": getattr(context, "aws_request_id", None),
        "correlationId": headers.get("x-correlation-id") or rc.get("requestId"),
        "durationMs": round((_time.monotonic() - started_at) * 1000, 2),
    }
    payload = json.dumps(record, default=str, separators=(",", ":"))
    getattr(logger, level)(payload)


def lambda_handler(event, context):
    import time as _time
    started_at = _time.monotonic()
    result = _lambda_handler_impl(event, context)
    _log_http_response(event or {}, context, result, started_at)
    return result

