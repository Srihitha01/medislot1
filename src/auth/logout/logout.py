
import hashlib
import json
import logging
import os

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --------------------------------------------------
# AWS CLIENTS
# --------------------------------------------------

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["AUTH_TOKENS_TABLE"]

table = ddb.Table(TABLE_NAME)


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
# LOGOUT HANDLER
# --------------------------------------------------

def lambda_handler(event, context):

    try:

        # ------------------------------------------
        # 1. READ AUTHORIZATION HEADER
        # ------------------------------------------

        headers = {
            str(key).lower(): value
            for key, value in (
                event.get("headers") or {}
            ).items()
        }

        authorization = headers.get(
            "authorization",
            ""
        )

        if (
            not isinstance(authorization, str)
            or not authorization.startswith("Bearer ")
        ):
            return response(
                401,
                {
                    "message": "Bearer access token is required"
                }
            )

        access_token = authorization[7:].strip()

        if not access_token:
            return response(
                401,
                {
                    "message": "Access token is missing"
                }
            )

        # ------------------------------------------
        # 2. VERIFY ACCESS TOKEN
        # ------------------------------------------

        claims = jwt.decode(
            access_token,
            secret(),
            algorithms=["HS256"],
            options={
                "require": [
                    "sub",
                    "role",
                    "exp",
                    "jti",
                    "sid"
                ]
            }
        )

        user_id = str(claims["sub"])
        session_id = str(claims["sid"])
        jti = str(claims["jti"])
        role = str(claims["role"])

        # ------------------------------------------
        # 3. READ REQUEST BODY
        # ------------------------------------------

        raw_body = event.get("body") or "{}"

        data = (
            json.loads(raw_body)
            if isinstance(raw_body, str)
            else raw_body
        )

        if not isinstance(data, dict):
            return response(
                400,
                {
                    "message": "Invalid request body"
                }
            )

        refresh_token = data.get("refreshToken")

        if (
            not isinstance(refresh_token, str)
            or not refresh_token.strip()
        ):
            return response(
                400,
                {
                    "message": "Refresh token is required"
                }
            )

        refresh_token = refresh_token.strip()

        # ------------------------------------------
        # 4. GET ACTIVE SESSION
        # ------------------------------------------

        active_key = f"ACTIVE#{user_id}"

        active_response = table.get_item(
            Key={
                "tokenId": active_key
            },
            ConsistentRead=True
        )

        active = active_response.get("Item")

        if not active:
            return response(
                401,
                {
                    "message": "Login session is no longer active"
                }
            )

        if (
            active.get("userId") != user_id
            or active.get("sessionId") != session_id
            or active.get("jti") != jti
            or active.get("tokenType") != "ACTIVE_ACCESS"
        ):
            return response(
                401,
                {
                    "message": "Login session does not match"
                }
            )

        # ------------------------------------------
        # 5. VERIFY REFRESH TOKEN
        # ------------------------------------------

        refresh_key = hash_token(refresh_token)

        refresh_response = table.get_item(
            Key={
                "tokenId": refresh_key
            },
            ConsistentRead=True
        )

        refresh_item = refresh_response.get("Item")

        if not refresh_item:
            return response(
                401,
                {
                    "message": "Refresh token is invalid or expired"
                }
            )

        if (
            refresh_item.get("userId") != user_id
            or refresh_item.get("sessionId") != session_id
            or refresh_item.get("tokenType") != "REFRESH"
        ):
            return response(
                401,
                {
                    "message": "Refresh token does not belong to this session"
                }
            )

        # ------------------------------------------
        # 6. DELETE REFRESH TOKEN FIRST
        # ------------------------------------------

        table.delete_item(
            Key={
                "tokenId": refresh_key
            },
            ConditionExpression=(
                "sessionId = :sid AND userId = :uid "
                "AND tokenType = :type"
            ),
            ExpressionAttributeValues={
                ":sid": session_id,
                ":uid": user_id,
                ":type": "REFRESH"
            }
        )

        # ------------------------------------------
        # 7. DELETE ACTIVE SESSION
        # ------------------------------------------

        try:

            table.delete_item(
                Key={
                    "tokenId": active_key
                },
                ConditionExpression=(
                    "sessionId = :sid AND jti = :jti "
                    "AND userId = :uid"
                ),
                ExpressionAttributeValues={
                    ":sid": session_id,
                    ":jti": jti,
                    ":uid": user_id
                }
            )

        except Exception:

            # --------------------------------------
            # ROLLBACK REFRESH TOKEN
            # --------------------------------------

            logger.exception(
                "Active session deletion failed; "
                "attempting refresh-token rollback"
            )

            try:

                table.put_item(
                    Item=refresh_item,
                    ConditionExpression=(
                        "attribute_not_exists(tokenId)"
                    )
                )

                logger.info(
                    "Refresh token restored for user %s",
                    user_id
                )

            except Exception:

                logger.exception(
                    "CRITICAL: Refresh token rollback failed "
                    "for user %s",
                    user_id
                )

            raise

        # ------------------------------------------
        # 8. LOG SUCCESS
        # ------------------------------------------

        logger.info(
            json.dumps({
                "event": "logout_succeeded",
                "userId": user_id,
                "role": role
            })
        )

        return response(
            200,
            {
                "message": "Logout successful"
            }
        )

    # ----------------------------------------------
    # JWT ERRORS
    # ----------------------------------------------

    except jwt.ExpiredSignatureError:

        return response(
            401,
            {
                "message": "Access token has expired"
            }
        )

    except jwt.InvalidTokenError:

        return response(
            401,
            {
                "message": "Invalid access token"
            }
        )

    # ----------------------------------------------
    # REQUEST ERRORS
    # ----------------------------------------------

    except (
        json.JSONDecodeError,
        UnicodeDecodeError
    ):

        return response(
            400,
            {
                "message": "Invalid JSON request"
            }
        )

    # ----------------------------------------------
    # AWS ERRORS
    # ----------------------------------------------

    except ClientError as exc:

        error = exc.response.get("Error", {})

        logger.error(
            json.dumps({
                "event": "logout_dynamodb_failure",
                "errorCode": error.get("Code"),
                "errorMessage": error.get("Message"),
                "userId": locals().get("user_id"),
                "requestId": exc.response.get(
                    "ResponseMetadata", {}
                ).get("RequestId")
            })
        )

        if error.get("Code") == "ConditionalCheckFailedException":
            return response(
                401,
                {
                    "message": "Login session changed or is no longer active"
                }
            )

        return response(
            500,
            {
                "message": "Logout could not be completed"
            }
        )

    except BotoCoreError:

        logger.exception(
            "Logout AWS dependency failure"
        )

        return response(
            500,
            {
                "message": "Logout could not be completed"
            }
        )

    # ----------------------------------------------
    # UNEXPECTED ERRORS
    # ----------------------------------------------

    except Exception:

        logger.exception(
            "Unexpected logout failure"
        )

        return response(
            500,
            {
                "message": "Logout could not be completed"
            }
        )
