
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

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

TOKEN_TABLE_NAME = os.environ["AUTH_TOKENS_TABLE"]
tokens = ddb.Table(TOKEN_TABLE_NAME)

ACCESS_MIN = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))


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
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )
    return result["Parameter"]["Value"]


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
        host=param(
            os.environ.get(
                "DB_HOST_PARAMETER",
                f"/medislot/{environment}/db/host"
            )
        ),
        port=int(
            param(
                os.environ.get(
                    "DB_PORT_PARAMETER",
                    f"/medislot/{environment}/db/port"
                )
            )
        ),
        user=os.environ["DB_USER"],
        password=param(
            os.environ.get(
                "DB_PASSWORD_PARAMETER",
                f"/medislot/{environment}/db/app-password"
            ),
            True
        ),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=10,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )


def hash_token(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_session(user_id, role, access_jti, session_id,
                 access_exp, refresh, refresh_exp):

    refresh_key = hash_token(refresh)

    # First save the refresh-token record.
    tokens.put_item(
        Item={
            "tokenId": refresh_key,
            "tokenType": "REFRESH",
            "userId": user_id,
            "role": role,
            "sessionId": session_id,
            "expiresAt": refresh_exp
        }
    )

    try:
        # Keep one active access-token session per user.
        # A new login replaces the previous active session.
        tokens.put_item(
            Item={
                "tokenId": f"ACTIVE#{user_id}",
                "tokenType": "ACTIVE_ACCESS",
                "userId": user_id,
                "jti": access_jti,
                "sessionId": session_id,
                "expiresAt": access_exp
            }
        )

    except Exception:
        # Roll back the refresh record if the active-session
        # write fails. Do not delete another login's session.
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
                user_id
            )

        raise


def lambda_handler(event, context):

    try:
        raw = event.get("body") or "{}"

        data = (
            json.loads(raw)
            if isinstance(raw, str)
            else raw
        )

        if not isinstance(data, dict):
            return response(
                400,
                {"message": "Invalid request body"}
            )

        email = data.get("email")
        password = data.get("password")

        if (
            not isinstance(email, str)
            or not isinstance(password, str)
        ):
            return response(
                400,
                {"message": "Email and password are required"}
            )

        email = email.strip().lower()

        if not re.fullmatch(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            email
        ):
            return response(
                400,
                {"message": "Invalid email address"}
            )

        # Verify credentials against RDS.
        conn = db()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        user_id,
                        public_user_id,
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
            return response(
                401,
                {"message": "Invalid email or password"}
            )

        stored_hash = user["password_hash"]

        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        if not bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash
        ):
            return response(
                401,
                {"message": "Invalid email or password"}
            )

        # Generate UUID-based identity and session.
        now = int(time.time())

        user_id = str(user["user_id"])  # immutable internal identity
        public_user_id = int(user["public_user_id"])
        role = user["role"]

        access_jti = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        access_exp = now + ACCESS_MIN * 60
        refresh_exp = now + REFRESH_DAYS * 86400

        access = jwt.encode(
            {
                "sub": user_id,
                "publicUserId": public_user_id,
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

        # Persist session records in DynamoDB.
        save_session(
            user_id=user_id,
            role=role,
            access_jti=access_jti,
            session_id=session_id,
            access_exp=access_exp,
            refresh=refresh,
            refresh_exp=refresh_exp
        )

        logger.info(
            json.dumps({
                "event": "login_succeeded",
                "userId": public_user_id,
                "internalUserId": user_id,
                "role": role
            })
        )

        return response(
            200,
            {
                "accessToken": access,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_MIN * 60,
                "refreshToken": refresh,
                "refreshExpiresIn": REFRESH_DAYS * 86400,
                "userId": public_user_id,
                "role": role
            }
        )

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(
            400,
            {"message": "Invalid JSON request"}
        )

    except (BotoCoreError, ClientError, pymysql.MySQLError):
        logger.exception("Login dependency failure")

        return response(
            500,
            {"message": "Login could not be completed"}
        )

    except Exception:
        logger.exception("Login failure")

        return response(
            500,
            {"message": "Login could not be completed"}
        )
