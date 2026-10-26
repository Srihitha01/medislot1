
import hashlib
import json
import logging
import os
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


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def get_jwt_secret():
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


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lambda_handler(event, context):
    correlation_id = getattr(context, "aws_request_id", str(uuid.uuid4()))

    try:
        headers = {
            str(key).lower(): value
            for key, value in (event.get("headers") or {}).items()
        }
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return response(401, {"message": "Bearer access token is required"})

        access_token = authorization[7:].strip()
        try:
            claims = jwt.decode(
                access_token,
                get_jwt_secret(),
                algorithms=["HS256"],
                options={"require": ["sub", "role", "iat", "exp", "jti"]},
            )
        except jwt.InvalidTokenError:
            return response(401, {"message": "Invalid or expired access token"})

        now = int(time.time())
        jti = claims.get("jti")
        expiry = int(claims.get("exp", 0))
        if not jti or expiry <= now:
            return response(401, {"message": "Invalid or expired access token"})

        # Denylist this access token for its remaining lifetime.
        table.put_item(
            Item={
                "tokenId": str(jti),
                "tokenType": "ACCESS_DENYLIST",
                "userId": str(claims["sub"]),
                "expiresAt": expiry,
            },
        )

        raw_body = event.get("body") or "{}"
        data = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        refresh_token = data.get("refreshToken") if isinstance(data, dict) else None

        if isinstance(refresh_token, str) and refresh_token:
            table.delete_item(
                Key={"tokenId": token_hash(refresh_token)},
                ConditionExpression="attribute_exists(tokenId)",
            )

        logger.info(json.dumps({
            "event": "logout_succeeded",
            "correlation_id": correlation_id,
        }))

        return response(200, {"message": "Logout successful"})

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {"message": "Invalid JSON request"})
    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({
            "event": "logout_dependency_error",
            "correlation_id": correlation_id,
        }))
        return response(500, {"message": "Logout could not be completed"})
    except Exception:
        logger.exception(json.dumps({
            "event": "logout_failed",
            "correlation_id": correlation_id,
        }))
        return response(500, {"message": "Logout could not be completed"})
