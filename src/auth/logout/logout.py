
import hashlib
import json
import logging
import os
import re

import boto3
import jwt

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

table = ddb.Table(os.environ["AUTH_TOKENS_TABLE"])

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


def _lambda_handler_impl(event, context):
    try:
        headers = {
            str(key).lower(): value
            for key, value in (event.get("headers") or {}).items()
        }

        authorization = headers.get("authorization", "")

        if not isinstance(authorization, str):
            return response(401, {
                "message": "Bearer access token is required"
            })

        parts = authorization.strip().split()

        if len(parts) != 2 or parts[0].lower() != "bearer":
            return response(401, {
                "message": "Bearer access token is required"
            })

        access_token = parts[1]

        claims = jwt.decode(
            access_token,
            secret(),
            algorithms=["HS256"],
            options={
                "require": ["sub", "role", "exp", "jti", "sid"]
            }
        )

        user_id = validate_user_id(claims["sub"])
        user_id_text = str(user_id)
        session_id = str(claims["sid"])
        jti = str(claims["jti"])
        role = str(claims["role"])

        raw_body = event.get("body") or "{}"
        data = (
            json.loads(raw_body)
            if isinstance(raw_body, str)
            else raw_body
        )

        if not isinstance(data, dict):
            return response(400, {"message": "Invalid request body"})

        refresh_token = data.get("refreshToken")

        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return response(400, {
                "message": "Refresh token is required"
            })

        refresh_token = refresh_token.strip()

        active_key = f"ACTIVE#{user_id_text}"

        active = table.get_item(
            Key={"tokenId": active_key},
            ConsistentRead=True
        ).get("Item")

        if not active:
            return response(401, {
                "message": "Login session is no longer active"
            })

        if (
            str(active.get("userId")) != user_id_text
            or active.get("sessionId") != session_id
            or active.get("jti") != jti
            or active.get("tokenType") != "ACTIVE_ACCESS"
        ):
            return response(401, {
                "message": "Login session does not match"
            })

        refresh_key = hash_token(refresh_token)

        refresh_item = table.get_item(
            Key={"tokenId": refresh_key},
            ConsistentRead=True
        ).get("Item")

        if not refresh_item:
            return response(401, {
                "message": "Refresh token is invalid or expired"
            })

        if (
            str(refresh_item.get("userId")) != user_id_text
            or refresh_item.get("sessionId") != session_id
            or refresh_item.get("tokenType") != "REFRESH"
        ):
            return response(401, {
                "message": "Refresh token does not belong to this session"
            })

        # Delete the refresh token first.
        table.delete_item(
            Key={"tokenId": refresh_key},
            ConditionExpression=(
                "sessionId = :sid AND userId = :uid "
                "AND tokenType = :type"
            ),
            ExpressionAttributeValues={
                ":sid": session_id,
                ":uid": user_id_text,
                ":type": "REFRESH"
            }
        )

        try:
            table.delete_item(
                Key={"tokenId": active_key},
                ConditionExpression=(
                    "sessionId = :sid AND jti = :jti "
                    "AND userId = :uid AND tokenType = :type"
                ),
                ExpressionAttributeValues={
                    ":sid": session_id,
                    ":jti": jti,
                    ":uid": user_id_text,
                    ":type": "ACTIVE_ACCESS"
                }
            )

        except Exception:
            logger.exception(
                "Active session deletion failed; attempting refresh rollback"
            )

            try:
                table.put_item(
                    Item=refresh_item,
                    ConditionExpression="attribute_not_exists(tokenId)"
                )
                logger.info(
                    "Refresh token restored for user %s",
                    user_id_text
                )
            except Exception:
                logger.exception(
                    "CRITICAL: Refresh token rollback failed for user %s",
                    user_id_text
                )

            raise

        logger.info(json.dumps({
            "event": "logout_succeeded",
            "userId": user_id,
            "role": role
        }))

        return response(200, {"message": "Logout successful"})

    except jwt.ExpiredSignatureError:
        return response(401, {
            "message": "Access token has expired"
        })

    except jwt.InvalidTokenError:
        return response(401, {
            "message": "Invalid access token"
        })

    except ValueError:
        return response(401, {
            "message": "Invalid numeric user identity"
        })

    except (json.JSONDecodeError, UnicodeDecodeError):
        return response(400, {"message": "Invalid JSON request"})

    except ClientError as exc:
        error = exc.response.get("Error", {})
        error_code = error.get("Code")

        if error_code == "ConditionalCheckFailedException":
            logger.warning(json.dumps({
                "event": "logout_session_already_inactive",
                "outcome": "expected_authentication_rejection",
                "statusCode": 401,
                "errorCode": error_code,
            }))
            return response(401, {
                "message": "Login session changed or is no longer active"
            })

        logger.exception(json.dumps({
            "event": "logout_dynamodb_failure",
            "statusCode": 500,
            "errorCode": error_code,
        }))
        return response(500, {
            "message": "Logout could not be completed"
        })

    except BotoCoreError:
        logger.exception("Logout AWS dependency failure")
        return response(500, {
            "message": "Logout could not be completed"
        })

    except Exception:
        logger.exception("Unexpected logout failure")
        return response(500, {
            "message": "Logout could not be completed"
        })

def _log_http_response(event, context, result, started_at):
    """Emit one safe, structured response record for every HTTP invocation."""
    import time as _time
    status = int((result or {}).get("statusCode", 500))
    if 200 <= status < 300:
        level, outcome = "info", "success"
    elif status in (401, 403):
        level, outcome = "warning", "access_denied"
    elif status == 409:
        level, outcome = "info", "business_conflict"
    elif status == 404:
        level, outcome = "info", "resource_not_found"
    elif 400 <= status < 500:
        level, outcome = "warning", "client_rejected"
    else:
        level, outcome = "error", "server_failure"
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    record = {
        "event": "http_response",
        "service": 'auth-logout',
        "method": event.get("httpMethod") or http.get("method"),
        "path": event.get("path") or event.get("rawPath"),
        "statusCode": status,
        "statusDescription": (
            "Request processed successfully" if 200 <= status < 300 else
            "Authentication is missing, invalid, expired, or inactive" if status == 401 else
            "Authenticated user is not permitted to perform this operation" if status == 403 else
            "Requested resource or route was not found" if status == 404 else
            "Request conflicts with the current resource or idempotency state" if status == 409 else
            "Request validation or method check was rejected" if 400 <= status < 500 else
            "Unexpected application or dependency failure"
        ),
        "outcome": outcome,
        "requestId": getattr(context, "aws_request_id", None),
        "correlationId": headers.get("x-correlation-id") or rc.get("requestId"),
        "durationMs": round((_time.monotonic() - started_at) * 1000, 2),
    }
    payload = json.dumps(record, default=str, separators=(",", ":"))
    getattr(logger, level)(payload)


def lambda_handler(event, context):
    import time as _time
    started_at = _time.monotonic()
    result = _lambda_handler_impl(event, context)
    _log_http_response(event or {}, context, result, started_at)
    return result

