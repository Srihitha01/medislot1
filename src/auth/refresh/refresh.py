
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

    return ssm.get_parameter(
        Name=name,
        WithDecryption=True,
    )["Parameter"]["Value"]


def lambda_handler(event, context):
    correlation_id = getattr(
        context, "aws_request_id", str(uuid.uuid4())
    )

    try:
        # 1. Parse and validate the request body.
        raw_body = event.get("body") or "{}"

        if isinstance(raw_body, str):
            data = json.loads(raw_body)
        else:
            data = raw_body

        if not isinstance(data, dict):
            return response(400, {"message": "Invalid request body"})

        old_refresh = data.get("refreshToken")

        if not isinstance(old_refresh, str) or not old_refresh.strip():
            return response(
                400,
                {"message": "refreshToken is required"},
            )

        # 2. Find the stored refresh token by its SHA-256 hash.
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

        # 3. Validate the expiry value returned by DynamoDB.
        try:
            expires_at = int(item["expiresAt"])
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.error(json.dumps({
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

        # 4. Read the signing secret before consuming the old token.
        # This avoids rotating the token if SSM cannot provide the secret.
        secret = jwt_secret()

        # 5. Prepare the replacement refresh token.
        new_refresh = secrets.token_urlsafe(48)
        new_key = token_hash(new_refresh)
        new_refresh_expiry = (
            now + REFRESH_TOKEN_DAYS * 24 * 60 * 60
        )

        # 6. Prepare the access token.
        access_expiry = now + ACCESS_TOKEN_MINUTES * 60
        jti = str(uuid.uuid4())

        payload = {
            "sub": str(item["userId"]),
            "role": str(item["role"]),
            "iat": now,
            "exp": access_expiry,
            "jti": jti,
        }

        access_token = jwt.encode(
            payload,
            secret,
            algorithm="HS256",
        )

        # 7. Atomically consume the old refresh token and create
        # the replacement. Use low-level DynamoDB AttributeValues.
        #
        # IMPORTANT:
        # Expiry was validated above. We intentionally do not compare
        # expiresAt in this transaction because DynamoDB reports that
        # the stored operand has type M (map), which cannot be compared
        # using >. The conditional delete still prevents reuse of a
        # token that has already been consumed.
        table.meta.client.transact_write_items(
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

        return response(200, {
            "accessToken": access_token,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_TOKEN_MINUTES * 60,
            "refreshToken": new_refresh,
            "refreshExpiresIn": REFRESH_TOKEN_DAYS * 24 * 60 * 60,
        })

    except ClientError as exc:
        error_code = exc.response.get(
            "Error", {}
        ).get("Code", "Unknown")

        if error_code == "TransactionCanceledException":
            logger.warning(json.dumps({
                "event": "refresh_transaction_cancelled",
                "correlation_id": correlation_id,
            }))
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
