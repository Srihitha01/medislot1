
import hashlib
import json
import logging
import os

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["AUTH_TOKENS_TABLE"]
table = ddb.Table(TABLE_NAME)
client = ddb.meta.client


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers":
                "Content-Type,Authorization,Idempotency-Key,X-Correlation-Id",
            "Access-Control-Allow-Methods":
                "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        },
        "body": json.dumps(body)
    }


def secret():
    env = os.environ["ENVIRONMENT"]
    name = os.environ.get(
        "JWT_SECRET_PARAMETER",
        f"/medislot/{env}/auth/jwt-secret"
    )

    return ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )["Parameter"]["Value"]


def hash_token(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def lambda_handler(event, context):
    try:
        headers = {
            str(k).lower(): v
            for k, v in (event.get("headers") or {}).items()
        }

        auth = headers.get("authorization", "")

        if not isinstance(auth, str) or not auth.startswith("Bearer "):
            return response(
                401,
                {"message": "Bearer access token is required"}
            )

        claims = jwt.decode(
            auth[7:].strip(),
            secret(),
            algorithms=["HS256"],
            options={
                "require": ["sub", "role", "exp", "jti", "sid"]
            }
        )

        user_id = str(claims["sub"])
        session_id = str(claims["sid"])
        jti = str(claims["jti"])

        raw = event.get("body") or "{}"
        data = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(data, dict):
            return response(400, {"message": "Invalid request body"})

        refresh = data.get("refreshToken")

        active_key = f"ACTIVE#{user_id}"

        # Verify that the active session belongs to this access token.
        active = table.get_item(
            Key={"tokenId": active_key},
            ConsistentRead=True
        ).get("Item")

        if (
            not active
            or active.get("sessionId") != session_id
            or active.get("jti") != jti
        ):
            return response(
                401,
                {"message": "Login session is no longer active"}
            )

        # Prepare the active-session deletion.
        operations = [
            {
                "Delete": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "tokenId": {"S": active_key}
                    },
                    "ConditionExpression":
                        "sessionId = :sid AND jti = :jti",
                    "ExpressionAttributeValues": {
                        ":sid": {"S": session_id},
                        ":jti": {"S": jti}
                    }
                }
            }
        ]

        # If a refresh token is provided, validate its ownership
        # before deleting either record.
        if refresh is not None:
            if not isinstance(refresh, str) or not refresh:
                return response(
                    400,
                    {"message": "Invalid refresh token"}
                )

            refresh_key = hash_token(refresh)

            refresh_item = table.get_item(
                Key={"tokenId": refresh_key},
                ConsistentRead=True
            ).get("Item")

            if (
                not refresh_item
                or refresh_item.get("sessionId") != session_id
                or refresh_item.get("userId") != user_id
                or refresh_item.get("tokenType") != "REFRESH"
            ):
                return response(
                    401,
                    {"message": "Refresh token does not match this session"}
                )

            operations.append({
                "Delete": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "tokenId": {"S": refresh_key}
                    },
                    "ConditionExpression":
                        "sessionId = :sid AND userId = :uid",
                    "ExpressionAttributeValues": {
                        ":sid": {"S": session_id},
                        ":uid": {"S": user_id}
                    }
                }
            })

        # Atomic deletion: both records are deleted or neither is.
        client.transact_write_items(
            TransactItems=operations
        )

        logger.info(json.dumps({
            "event": "logout_succeeded",
            "userId": user_id,
            "role": claims["role"]
        }))

        return response(
            200,
            {"message": "Logout successful"}
        )

    except jwt.ExpiredSignatureError:
        return response(
            401,
            {"message": "Access token has expired"}
        )

    except jwt.InvalidTokenError:
        return response(
            401,
            {"message": "Invalid access token"}
        )

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(
            400,
            {"message": "Invalid JSON request"}
        )

    except ClientError as exc:
        logger.exception("Logout DynamoDB failure")

        if exc.response.get("Error", {}).get("Code") in (
            "TransactionCanceledException",
            "ConditionalCheckFailedException"
        ):
            return response(
                401,
                {"message": "Login session changed or is no longer active"}
            )

        return response(
            500,
            {"message": "Logout could not be completed"}
        )

    except (BotoCoreError, Exception):
        logger.exception("Logout failure")
        return response(
            500,
            {"message": "Logout could not be completed"}
        )
