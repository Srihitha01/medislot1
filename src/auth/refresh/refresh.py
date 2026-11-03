
import hashlib
import json
import logging
import os
import re
import secrets
import time

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

table = ddb.Table(os.environ["AUTH_TOKENS_TABLE"])

ACCESS_MIN = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))

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


def secret():
    environment = os.environ["ENVIRONMENT"]

    parameter_name = os.environ.get(
        "JWT_SECRET_PARAMETER",
        f"/medislot/{environment}/auth/jwt-secret"
    )

    return ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )["Parameter"]["Value"]


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


def lambda_handler(event, context):
    try:
        raw = event.get("body") or "{}"
        data = json.loads(raw) if isinstance(raw, str) else raw

        token = data.get("refreshToken") if isinstance(data, dict) else None

        if isinstance(token, str):
            token = token.strip()

        if not isinstance(token, str) or not token:
            return response(400, {
                "message": "refreshToken is required"
            })

        item = table.get_item(
            Key={"tokenId": hash_token(token)},
            ConsistentRead=True
        ).get("Item")

        now = int(time.time())

        if (
            not item
            or item.get("tokenType") != "REFRESH"
            or int(item.get("expiresAt", 0)) <= now
        ):
            return response(401, {
                "message": "Invalid or expired refresh token"
            })

        user_id = validate_user_id(item.get("userId"))
        user_id_text = str(user_id)
        session_id = str(item["sessionId"])

        active = table.get_item(
            Key={"tokenId": f"ACTIVE#{user_id_text}"},
            ConsistentRead=True
        ).get("Item")

        if (
            not active
            or active.get("userId") != user_id_text
            or active.get("sessionId") != session_id
            or active.get("tokenType") != "ACTIVE_ACCESS"
        ):
            return response(401, {
                "message": "This login session is no longer active"
            })

        jti = secrets.token_urlsafe(24)
        access_exp = now + ACCESS_MIN * 60

        access = jwt.encode(
            {
                "sub": user_id_text,
                "role": item["role"],
                "iat": now,
                "exp": access_exp,
                "jti": jti,
                "sid": session_id
            },
            secret(),
            algorithm="HS256"
        )

        # Conditional update prevents a stale session from
        # overwriting a newer login or logout.
        table.update_item(
            Key={"tokenId": f"ACTIVE#{user_id_text}"},
            UpdateExpression="SET jti = :j, expiresAt = :e",
            ConditionExpression=(
                "sessionId = :s AND tokenType = :t AND userId = :u"
            ),
            ExpressionAttributeValues={
                ":j": jti,
                ":e": access_exp,
                ":s": session_id,
                ":t": "ACTIVE_ACCESS",
                ":u": user_id_text
            }
        )

        return response(200, {
            "accessToken": access,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_MIN * 60,
            "refreshToken": token,
            "refreshExpiresIn": int(item["expiresAt"]) - now
        })

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {"message": "Invalid JSON request"})

    except ValueError:
        return response(401, {
            "message": "Invalid refresh-token user identity"
        })

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")

        if error_code == "ConditionalCheckFailedException":
            return response(401, {
                "message": "Login session is no longer active"
            })

        logger.exception("Refresh dependency failure")
        return response(500, {
            "message": "Token refresh could not be completed"
        })

    except BotoCoreError:
        logger.exception("Refresh AWS dependency failure")
        return response(500, {
            "message": "Token refresh could not be completed"
        })

    except Exception:
        logger.exception("Refresh failure")
        return response(500, {
            "message": "Token refresh could not be completed"
        })
