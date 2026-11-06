
import hashlib
import json
import logging
import os
import re
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


# ---------------------------------------------------------
# API GATEWAY RESPONSE
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# JWT SIGNING SECRET
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# HASH REFRESH TOKEN
# ---------------------------------------------------------

def hash_token(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------
# VALIDATE USER ID
# ---------------------------------------------------------

def validate_user_id(value):
    if isinstance(value, bool):
        raise ValueError("Invalid numeric user ID")

    user_id = str(value)

    if not USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError("Invalid numeric user ID")

    numeric_id = int(user_id)

    if numeric_id > MAX_UNSIGNED_BIGINT:
        raise ValueError(
            "User ID exceeds BIGINT UNSIGNED limit"
        )

    return numeric_id


# ---------------------------------------------------------
# REFRESH ACCESS TOKEN
# ---------------------------------------------------------

def lambda_handler(event, context):
    try:
        # Step 1: Parse request body.
        raw = event.get("body") or "{}"

        data = (
            json.loads(raw)
            if isinstance(raw, str)
            else raw
        )

        token = (
            data.get("refreshToken")
            if isinstance(data, dict)
            else None
        )

        if isinstance(token, str):
            token = token.strip()

        if not isinstance(token, str) or not token:
            return response(400, {
                "message": "refreshToken is required"
            })

        # Step 2: Read refresh-token record.
        item = table.get_item(
            Key={
                "tokenId": hash_token(token)
            },
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

        # Step 3: Validate user and session identity.
        user_id = validate_user_id(item.get("userId"))
        user_id_text = str(user_id)

        session_id = item.get("sessionId")
        role = item.get("role")

        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(role, str)
            or not role
        ):
            return response(401, {
                "message": "Invalid refresh-token session"
            })

        # Step 4: Read active session.
        active_key = f"ACTIVE#{user_id_text}"

        active = table.get_item(
            Key={
                "tokenId": active_key
            },
            ConsistentRead=True
        ).get("Item")

        if (
            not active
            or str(active.get("userId")) != user_id_text
            or active.get("sessionId") != session_id
            or active.get("tokenType") != "ACTIVE_ACCESS"
        ):
            return response(401, {
                "message": "This login session is no longer active"
            })

        # Step 5: Reuse the active session's JWT ID.
        #
        # IMPORTANT:
        # Do not generate a new jti on every refresh.
        #
        # The authorizer requires:
        #
        #     JWT jti == DynamoDB ACTIVE session jti
        #
        # Reusing the jti prevents concurrent refresh
        # requests from invalidating each other's
        # newly issued access tokens.
        jti = active.get("jti")

        if not isinstance(jti, str) or not jti:
            return response(401, {
                "message": "Active session has no valid JWT ID"
            })

        # Step 6: Calculate new access-token expiry.
        access_exp = now + ACCESS_MIN * 60

        # Step 7: Extend active-session expiry.
        #
        # Keep jti unchanged.
        #
        # The conditional update ensures that a
        # different login or revoked session cannot
        # be silently overwritten.
        table.update_item(
            Key={
                "tokenId": active_key
            },
            UpdateExpression=(
                "SET expiresAt = :e"
            ),
            ConditionExpression=(
                "sessionId = :s "
                "AND tokenType = :t "
                "AND userId = :u "
                "AND jti = :j"
            ),
            ExpressionAttributeValues={
                ":e": access_exp,
                ":s": session_id,
                ":t": "ACTIVE_ACCESS",
                ":u": user_id_text,
                ":j": jti
            }
        )

        # Step 8: Generate new JWT with the SAME jti.
        access = jwt.encode(
            {
                "sub": user_id_text,
                "role": role,
                "iat": now,
                "exp": access_exp,
                "jti": jti,
                "sid": session_id
            },
            secret(),
            algorithm="HS256"
        )

        logger.info(
            "Access token refreshed for user_id=%s",
            user_id_text
        )

        # Step 9: Return refreshed access token.
        return response(200, {
            "accessToken": access,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_MIN * 60,
            "refreshToken": token,
            "refreshExpiresIn": (
                int(item["expiresAt"]) - now
            )
        })

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {
            "message": "Invalid JSON request"
        })

    except ValueError:
        return response(401, {
            "message": "Invalid refresh-token user identity"
        })

    except ClientError as exc:
        error_code = exc.response.get(
            "Error", {}
        ).get("Code")

        if error_code == "ConditionalCheckFailedException":
            return response(401, {
                "message": "Login session is no longer active"
            })

        logger.exception(
            "Refresh dependency failure"
        )

        return response(500, {
            "message": "Token refresh could not be completed"
        })

    except BotoCoreError:
        logger.exception(
            "Refresh AWS dependency failure"
        )

        return response(500, {
            "message": "Token refresh could not be completed"
        })

    except Exception:
        logger.exception("Refresh failure")

        return response(500, {
            "message": "Token refresh could not be completed"
        })