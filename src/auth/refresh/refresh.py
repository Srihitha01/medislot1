import hashlib
import json
import logging
import os
import time
import uuid

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --------------------------------------------------
# AWS CLIENTS AND SETTINGS
# --------------------------------------------------

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

table = ddb.Table(os.environ["AUTH_TOKENS_TABLE"])

ACCESS_MIN = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))


# --------------------------------------------------
# RESPONSE
# --------------------------------------------------

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


# --------------------------------------------------
# JWT SECRET
# --------------------------------------------------

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


# --------------------------------------------------
# TOKEN HASH
# --------------------------------------------------

def hash_token(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


# --------------------------------------------------
# REFRESH HANDLER
# --------------------------------------------------

def lambda_handler(event, context):

    try:

        # ------------------------------------------
        # 1. READ REQUEST BODY
        # ------------------------------------------

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

        # Remove stray spaces or line breaks from copy-paste.
        if isinstance(token, str):
            token = token.strip()

        if not isinstance(token, str) or not token:
            return response(
                400,
                {"message": "refreshToken is required"}
            )

        # ------------------------------------------
        # 2. LOOK UP THE REFRESH TOKEN
        # ------------------------------------------

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
            return response(
                401,
                {"message": "Invalid or expired refresh token"}
            )

        user_id = str(item["userId"])
        session_id = str(item["sessionId"])

        # ------------------------------------------
        # 3. CHECK THE LOGIN SESSION IS STILL ACTIVE
        # ------------------------------------------

        active = table.get_item(
            Key={"tokenId": f"ACTIVE#{user_id}"},
            ConsistentRead=True
        ).get("Item")

        if not active or active.get("sessionId") != session_id:
            return response(
                401,
                {"message": "This login session is no longer active"}
            )

        # ------------------------------------------
        # 4. ISSUE A NEW ACCESS TOKEN
        # ------------------------------------------

        jti = str(uuid.uuid4())
        access_exp = now + ACCESS_MIN * 60

        access = jwt.encode(
            {
                "sub": user_id,
                "role": item["role"],
                "iat": now,
                "exp": access_exp,
                "jti": jti,
                "sid": session_id
            },
            secret(),
            algorithm="HS256"
        )

        # ------------------------------------------
        # 5. SAVE THE NEW ACCESS TOKEN ID
        # ------------------------------------------
        # The condition stops a stale refresh request
        # from overwriting a newer login.

        table.update_item(
            Key={"tokenId": f"ACTIVE#{user_id}"},
            UpdateExpression="SET jti = :j, expiresAt = :e",
            ConditionExpression=(
                "sessionId = :s AND tokenType = :t"
            ),
            ExpressionAttributeValues={
                ":j": jti,
                ":e": access_exp,
                ":s": session_id,
                ":t": "ACTIVE_ACCESS"
            }
        )

        # ------------------------------------------
        # 6. RETURN THE NEW ACCESS TOKEN
        # ------------------------------------------

        return response(
            200,
            {
                "accessToken": access,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_MIN * 60,
                "refreshToken": token,
                "refreshExpiresIn": int(item["expiresAt"]) - now
            }
        )

    # ----------------------------------------------
    # REQUEST ERRORS
    # ----------------------------------------------

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(
            400,
            {"message": "Invalid JSON request"}
        )

    # ----------------------------------------------
    # AWS ERRORS
    # ----------------------------------------------

    except ClientError as exc:

        error_code = exc.response.get("Error", {}).get("Code")

        if error_code == "ConditionalCheckFailedException":
            return response(
                401,
                {"message": "Login session is no longer active"}
            )

        logger.exception("Refresh dependency failure")

        return response(
            500,
            {"message": "Token refresh could not be completed"}
        )

    except BotoCoreError:

        logger.exception("Refresh dependency failure")

        return response(
            500,
            {"message": "Token refresh could not be completed"}
        )

    # ----------------------------------------------
    # UNEXPECTED ERRORS
    # ----------------------------------------------

    except Exception:

        logger.exception("Refresh failure")

        return response(
            500,
            {"message": "Token refresh could not be completed"}
        )