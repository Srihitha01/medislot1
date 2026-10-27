
import json
import logging
import os

import boto3
import jwt
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["AUTH_TOKENS_TABLE"])


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


def policy(principal_id, effect, resource, user_id=None, role=None):
    statement = {
        "Action": "execute-api:Invoke",
        "Effect": effect,
        "Resource": resource,
    }
    result = {
        "principalId": str(principal_id),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [statement],
        },
    }

    if effect == "Allow" and user_id is not None and role is not None:
        result["context"] = {
            "userId": str(user_id),
            "role": str(role),
        }

    return result


def extract_token(event):
    value = event.get("authorizationToken")

    if not value:
        headers = event.get("headers") or {}
        value = next(
            (
                v for k, v in headers.items()
                if k.lower() == "authorization"
            ),
            None,
        )

    if not isinstance(value, str) or not value.startswith("Bearer "):
        return None

    token = value[7:].strip()
    return token or None


def lambda_handler(event, context):
    method_arn = event.get("methodArn", "*")
    token = extract_token(event)

    if not token:
        raise Exception("Unauthorized")

    try:
        claims = jwt.decode(
            token,
            get_jwt_secret(),
            algorithms=["HS256"],
            options={"require": ["sub", "role", "iat", "exp", "jti"]},
        )

        user_id = claims.get("sub")
        role = claims.get("role")
        jti = claims.get("jti")

        if (
            not isinstance(user_id, str)
            or not user_id
            or role not in {"doctor", "patient"}
            or not isinstance(jti, str)
            or not jti
        ):
            raise jwt.InvalidTokenError("Invalid required claims")

        # A token is denied if logout previously recorded its jti.
        result = table.get_item(
            Key={"tokenId": jti},
            ConsistentRead=True,
        )
        revoked = result.get("Item")

        if revoked and revoked.get("tokenType") == "ACCESS_DENYLIST":
            raise jwt.InvalidTokenError("Token has been revoked")

        logger.info(json.dumps({
            "event": "authorization_succeeded",
            "role": role,
        }))

        return policy(
            principal_id=user_id,
            effect="Allow",
            resource=method_arn,
            user_id=user_id,
            role=role,
        )

    except jwt.InvalidTokenError:
        raise Exception("Unauthorized")
    except (BotoCoreError, ClientError):
        # Fail closed: dependency failures must not grant access.
        logger.exception(json.dumps({
            "event": "authorization_dependency_error",
        }))
        raise Exception("Unauthorized")
