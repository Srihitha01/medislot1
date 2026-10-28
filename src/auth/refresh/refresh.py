import hashlib
import json
import logging
import os
import secrets
import time
import uuid

import boto3
import jwt
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
# Low-level client for transact_write_items, which uses explicit DynamoDB types
# such as {"S": "value"}. The resource's own client would serialize those typed
# values a second time and DynamoDB would reject the key.
dynamodb_client = boto3.client("dynamodb")

table = dynamodb.Table(os.environ["AUTH_TOKENS_TABLE"])

ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def jwt_secret():
    environment = os.environ["ENVIRONMENT"]

    name = os.environ.get(
        "JWT_SECRET_PARAMETER",
        f"/medislot/{environment}/auth/jwt-secret",
    )

    version = os.environ.get("JWT_SECRET_PARAMETER_VERSION")

    if version:
        name = f"{name}:{version}"

    result = ssm.get_parameter(
        Name=name,
        WithDecryption=True,
    )

    return result["Parameter"]["Value"]


def lambda_handler(event, context):
    correlation_id = getattr(
        context,
        "aws_request_id",
        str(uuid.uuid4()),
    )

    try:
        # 1. Parse request body.
        raw_body = event.get("body") or "{}"

        if isinstance(raw_body, str):
            data = json.loads(raw_body)
        else:
            data = raw_body

        if not isinstance(data, dict):
            return response(
                400,
                {"message": "Invalid request body"},
            )

        old_refresh = data.get("refreshToken")

        if not isinstance(old_refresh, str) or not old_refresh.strip():
            return response(
                400,
                {"message": "refreshToken is required"},
            )

        # 2. Look up the hashed refresh token.
        old_key = token_hash(old_refresh)

        result = table.get_item(
            Key={"tokenId": old_key},
            ConsistentRead=True,
        )

        item = result.get("Item")
        now = int(time.time())

        if not item or item.get("tokenType") != "REFRESH":
            return response(
                401,
                {"message": "Invalid or expired refresh token"},
            )

        # 3. Validate expiration.
        try:
            expires_at = int(item["expiresAt"])
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.warning(json.dumps({
                "event": "refresh_invalid_expiry",
                "correlation_id": correlation_id,
            }))

            return response(
                401,
                {"message": "Invalid or expired refresh token"},
            )

        if expires_at <= now:
            return response(
                401,
                {"message": "Invalid or expired refresh token"},
            )

        if not item.get("userId") or not item.get("role"):
            logger.error(json.dumps({
                "event": "refresh_invalid_token_record",
                "correlation_id": correlation_id,
            }))

            return response(
                401,
                {"message": "Invalid or expired refresh token"},
            )

        # 4. Retrieve the signing secret before consuming the token.
        secret = jwt_secret()

        # Warn without exposing the secret.
        if len(secret.encode("utf-8")) < 32:
            logger.error(json.dumps({
                "event": "jwt_signing_secret_too_short",
                "correlation_id": correlation_id,
                "secret_length_bytes": len(secret.encode("utf-8")),
            }))

            return response(
                500,
                {"message": "Authentication configuration error"},
            )

        # 5. Generate replacement refresh token.
        new_refresh = secrets.token_urlsafe(48)
        new_key = token_hash(new_refresh)

        new_refresh_expiry = (
            now + REFRESH_TOKEN_DAYS * 24 * 60 * 60
        )

        # 6. Generate access token.
        access_expiry = now + ACCESS_TOKEN_MINUTES * 60

        payload = {
            "sub": str(item["userId"]),
            "role": str(item["role"]),
            "iat": now,
            "exp": access_expiry,
            "jti": str(uuid.uuid4()),
        }

        access_token = jwt.encode(
            payload,
            secret,
            algorithm="HS256",
        )

        # 7. Atomically consume the old refresh token and insert
        # the new one. The expiry value was validated above.
        dynamodb_client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": table.name,
                        "Key": {
                            "tokenId": {"S": old_key},
                        },
                        "ConditionExpression": (
                            "attribute_exists(tokenId) AND "
                            "tokenType = :refresh"
                        ),
                        "ExpressionAttributeValues": {
                            ":refresh": {"S": "REFRESH"},
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            "tokenId": {"S": new_key},
                            "tokenType": {"S": "REFRESH"},
                            "userId": {"S": str(item["userId"])},
                            "role": {"S": str(item["role"])},
                            "expiresAt": {
                                "N": str(new_refresh_expiry),
                            },
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(tokenId)"
                        ),
                    }
                },
            ]
        )

        logger.info(json.dumps({
            "event": "refresh_succeeded",
            "correlation_id": correlation_id,
            "role": str(item["role"]),
        }))

        return response(
            200,
            {
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_TOKEN_MINUTES * 60,
                "refreshToken": new_refresh,
                "refreshExpiresIn": (
                    REFRESH_TOKEN_DAYS * 24 * 60 * 60
                ),
            },
        )

    except ClientError as exc:
        error = exc.response.get("Error", {})
        error_code = error.get("Code", "Unknown")

        if error_code == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons", [])

            logger.warning(json.dumps({
                "event": "refresh_transaction_cancelled",
                "correlation_id": correlation_id,
                "cancellation_reasons": [
                    {
                        "code": reason.get("Code"),
                        "message": reason.get("Message"),
                    }
                    for reason in reasons
                ],
            }))

            # Do not expose internal DynamoDB details to the client.
            return response(
                401,
                {"message": "Invalid or already-used refresh token"},
            )

        logger.exception(json.dumps({
            "event": "refresh_dependency_error",
            "correlation_id": correlation_id,
            "error_code": error_code,
        }))

        return response(
            500,
            {"message": "Token refresh could not be completed"},
        )

    except BotoCoreError:
        logger.exception(json.dumps({
            "event": "refresh_dependency_error",
            "correlation_id": correlation_id,
        }))

        return response(
            500,
            {"message": "Token refresh could not be completed"},
        )

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(
            400,
            {"message": "Invalid JSON request"},
        )

    except Exception:
        logger.exception(json.dumps({
            "event": "refresh_failed",
            "correlation_id": correlation_id,
        }))

        return response(
            500,
            {"message": "Token refresh could not be completed"},
        )