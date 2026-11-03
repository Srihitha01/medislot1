
import logging
import os
import re

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")

JWT_SECRET_PARAMETER = os.getenv(
    "JWT_SECRET_PARAMETER",
    f"/medislot/{ENVIRONMENT}/auth/jwt-secret"
)

AUTH_TABLE_NAME = os.getenv(
    "AUTH_TABLE_NAME",
    os.getenv("AUTH_TOKENS_TABLE", "")
)

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

USER_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
MAX_UNSIGNED_BIGINT = 18446744073709551615


# ---------------------------------------------------------
# LAZY DYNAMODB TABLE
# ---------------------------------------------------------

class DynamoDBTableProxy:
    """
    Creates the DynamoDB connection only when required.
    This prevents AWS credential errors during module import
    and allows unittest.mock.patch.object(authorizer.table, ...).
    """

    def __init__(self):
        self._table = None

    def _get_table(self):
        if self._table is None:
            table_name = AUTH_TABLE_NAME or os.getenv(
                "AUTH_TOKENS_TABLE",
                ""
            )

            if not table_name:
                raise ValueError("AUTH_TABLE_NAME is not configured")

            dynamodb = boto3.resource(
                "dynamodb",
                region_name=AWS_REGION
            )

            self._table = dynamodb.Table(table_name)

        return self._table

    def get_item(self, **kwargs):
        return self._get_table().get_item(**kwargs)


# Keep this module-level object available for unit tests.
table = DynamoDBTableProxy()


# ---------------------------------------------------------
# GET JWT SECRET
# ---------------------------------------------------------

def get_secret():
    """
    Retrieve the JWT signing secret from SSM Parameter Store.
    """

    ssm = boto3.client(
        "ssm",
        region_name=AWS_REGION
    )

    response = ssm.get_parameter(
        Name=JWT_SECRET_PARAMETER,
        WithDecryption=True
    )

    return response["Parameter"]["Value"]


# Backward-compatible function name.
def get_jwt_secret():
    return get_secret()


# ---------------------------------------------------------
# VALIDATE NUMERIC USER ID
# ---------------------------------------------------------

def validate_user_id(value):
    """
    Accept only canonical positive ASCII numeric IDs.

    Accepted:
        1
        25
        100

    Rejected:
        0
        -1
        001
        1.5
        UUIDs
        SQL injection strings
        Non-ASCII digits
    """

    if isinstance(value, bool):
        raise ValueError("Invalid user ID")

    if not isinstance(value, (str, int)):
        raise ValueError("Invalid user ID")

    user_id = str(value)

    if not USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError("Invalid user ID")

    numeric_id = int(user_id)

    if numeric_id > MAX_UNSIGNED_BIGINT:
        raise ValueError("User ID exceeds BIGINT UNSIGNED limit")

    return numeric_id


# ---------------------------------------------------------
# EXTRACT BEARER TOKEN
# ---------------------------------------------------------

def get_bearer_token(event):
    """
    Supports API Gateway TOKEN authorizer events and
    API Gateway header-based events.
    """

    authorization = event.get("authorizationToken")

    if not authorization:
        headers = event.get("headers") or {}

        authorization = (
            headers.get("Authorization")
            or headers.get("authorization")
        )

    if not isinstance(authorization, str):
        raise ValueError("Missing Authorization token")

    parts = authorization.strip().split()

    if len(parts) != 2:
        raise ValueError("Invalid Authorization header")

    if parts[0].lower() != "bearer":
        raise ValueError("Authorization scheme must be Bearer")

    if not parts[1]:
        raise ValueError("Bearer token is empty")

    return parts[1]


# ---------------------------------------------------------
# VERIFY JWT
# ---------------------------------------------------------

def verify_jwt(token):
    """
    Verify JWT signature and required claims.
    """

    secret = get_secret()

    claims = jwt.decode(
        token,
        secret,
        algorithms=[JWT_ALGORITHM],
        options={
            "require": [
                "sub",
                "role",
                "iat",
                "exp",
                "jti",
                "sid"
            ],
            "verify_signature": True,
            "verify_exp": True
        }
    )

    user_id = validate_user_id(claims.get("sub"))

    role = claims.get("role")
    jwt_id = claims.get("jti")
    session_id = claims.get("sid")

    if not isinstance(role, str) or not role.strip():
        raise ValueError("Invalid user role")

    if not isinstance(jwt_id, str) or not jwt_id:
        raise ValueError("Invalid JWT ID")

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Invalid session ID")

    return claims, user_id, role


# ---------------------------------------------------------
# VERIFY ACTIVE SESSION
# ---------------------------------------------------------

def verify_active_session(user_id, claims):
    """
    Verify that the JWT belongs to the currently active
    DynamoDB session.

    DynamoDB partition key:
        tokenId

    Active session key:
        ACTIVE#{user_id}

    Required attributes:
        tokenType
        jti
        sessionId

    userId is checked when present.
    """

    session_key = f"ACTIVE#{user_id}"

    response = table.get_item(
        Key={
            "tokenId": session_key
        },
        ConsistentRead=True
    )

    item = response.get("Item")

    if not item:
        raise ValueError("Active session not found")

    if item.get("tokenType") != "ACTIVE_ACCESS":
        raise ValueError("Invalid session token type")

    # Validate JWT ID against the active access token.
    if str(item.get("jti", "")) != str(claims.get("jti", "")):
        raise ValueError("JWT ID does not match active session")

    # Validate session ID.
    if str(item.get("sessionId", "")) != str(claims.get("sid", "")):
        raise ValueError("Session ID does not match")

    # Validate user ID if stored in DynamoDB.
    stored_user_id = item.get("userId")

    if stored_user_id is not None:
        if str(stored_user_id) != str(user_id):
            raise ValueError("User ID does not match active session")

    return True


# ---------------------------------------------------------
# GENERATE API GATEWAY POLICY
# ---------------------------------------------------------

def generate_policy(
    principal_id,
    effect,
    resource,
    user_id,
    role
):
    return {
        "principalId": str(principal_id),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        },
        "context": {
            "userId": str(user_id),
            "role": str(role)
        }
    }


# ---------------------------------------------------------
# MAIN AUTHORIZER
# ---------------------------------------------------------

def authorizer(event, context):
    """
    Lambda authorizer entry point.

    Performs:
        1. Bearer token validation
        2. JWT signature validation
        3. Numeric user ID validation
        4. Active DynamoDB session validation
        5. jti validation
        6. sid validation
        7. API Gateway policy generation
    """

    try:
        logger.info("MediSlot authorizer started")

        # Step 1: Extract bearer token.
        token = get_bearer_token(event)

        # Step 2: Validate JWT.
        claims, user_id, role = verify_jwt(token)

        # Step 3: Validate active session.
        verify_active_session(user_id, claims)

        # Step 4: Get API Gateway resource ARN.
        method_arn = event.get("methodArn")

        if not method_arn:
            raise ValueError("Missing methodArn")

        logger.info(
            "Authorization successful for user_id=%s role=%s",
            user_id,
            role
        )

        # Step 5: Return Allow policy.
        return generate_policy(
            principal_id=user_id,
            effect="Allow",
            resource=method_arn,
            user_id=user_id,
            role=role
        )

    except (jwt.InvalidTokenError, ValueError) as exc:
        logger.warning("Authorization rejected: %s", str(exc))
        raise Exception("Unauthorized")

    except (BotoCoreError, ClientError):
        logger.exception("AWS service error during authorization")
        raise Exception("Unauthorized")

    except Exception:
        logger.exception("Unexpected authorizer error")
        raise Exception("Unauthorized")


# API Gateway Lambda handler.
def lambda_handler(event, context):
    return authorizer(event, context)
