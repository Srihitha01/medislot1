"""MediSlot application API router.

This handler implements slot discovery/creation and patient waitlist operations
against the DynamoDB tables declared by cloudformation/data-stack.yaml.
Appointment booking is intentionally not fabricated here because the current
infrastructure has no appointments table.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DDB = None


def _dynamodb():
    global _DDB
    if _DDB is None:
        _DDB = boto3.resource("dynamodb")
    return _DDB


def _table(env_name: str):
    name = os.environ.get(env_name)
    if not name:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return _dynamodb().Table(name)


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status: int, body: dict[str, Any]):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError("Request body must be a JSON object")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Request body must be a JSON object")
    return result


def _principal(event: dict[str, Any]) -> tuple[str | None, str | None]:
    context = (event.get("requestContext") or {}).get("authorizer") or {}
    user_id = context.get("userId") or context.get("userid")
    role = context.get("role")
    return (str(user_id) if user_id else None, str(role).lower() if role else None)


def _path(event: dict[str, Any]) -> str:
    path = event.get("path") or event.get("rawPath") or "/"
    path = "/" + str(path).strip("/") if str(path).strip("/") else "/"
    # In case a stage-prefixed path is passed by a local/test invocation.
    stage = os.environ.get("ENVIRONMENT", "dev")
    if path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1 :]
    elif path == f"/{stage}":
        path = "/"
    if path != "/":
        path = path.rstrip("/")
    return path


def _query(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in params.items() if v is not None}


def _iso_utc(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("startUtc must be an ISO-8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("startUtc must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("startUtc must include a timezone, for example Z")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scan_all(table, **kwargs):
    items: list[dict[str, Any]] = []
    response = table.scan(**kwargs)
    items.extend(response.get("Items", []))
    while response.get("LastEvaluatedKey"):
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
        items.extend(response.get("Items", []))
    return items


def _list_slots(event):
    table = _table("SLOTS_TABLE")
    params = _query(event)
    doctor_id = params.get("doctorId")
    start_from = params.get("startFrom")
    start_to = params.get("startTo")
    expression = Attr("status").eq("OPEN")

    if doctor_id:
        key_expr = Key("doctorId").eq(doctor_id)
        if start_from and start_to:
            key_expr &= Key("startUtc").between(_iso_utc(start_from), _iso_utc(start_to))
        elif start_from:
            key_expr &= Key("startUtc").gte(_iso_utc(start_from))
        elif start_to:
            key_expr &= Key("startUtc").lte(_iso_utc(start_to))
        result = table.query(
            IndexName="gsi-doctor-time",
            KeyConditionExpression=key_expr,
            FilterExpression=expression,
        )
        items = list(result.get("Items", []))
        while result.get("LastEvaluatedKey"):
            result = table.query(
                IndexName="gsi-doctor-time",
                KeyConditionExpression=key_expr,
                FilterExpression=expression,
                ExclusiveStartKey=result["LastEvaluatedKey"],
            )
            items.extend(result.get("Items", []))
    else:
        items = _scan_all(table, FilterExpression=expression)
        if start_from:
            items = [x for x in items if x.get("startUtc", "") >= _iso_utc(start_from)]
        if start_to:
            items = [x for x in items if x.get("startUtc", "") <= _iso_utc(start_to)]

    items.sort(key=lambda x: (x.get("startUtc", ""), x.get("slotId", "")))
    return _response(200, {"items": items, "count": len(items)})


def _create_slot(event, user_id, role):
    if role != "doctor":
        return _response(403, {"message": "Only doctors can create slots"})
    data = _body(event)
    try:
        start_utc = _iso_utc(data.get("startUtc"))
        duration = data.get("durationMinutes", 15)
        capacity = data.get("capacity", 1)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 180:
            raise ValueError("durationMinutes must be an integer between 5 and 180")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 50:
            raise ValueError("capacity must be an integer between 1 and 50")
    except ValueError as exc:
        return _response(400, {"message": str(exc)})

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    item = {
        "slotId": str(uuid.uuid4()),
        "doctorId": user_id,
        "startUtc": start_utc,
        "durationMinutes": duration,
        "capacity": capacity,
        "bookedCount": 0,
        "status": "OPEN",
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        _table("SLOTS_TABLE").put_item(
            Item=item,
            ConditionExpression=Attr("slotId").not_exists(),
        )
    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({"event": "slot_create_failed"}))
        return _response(500, {"message": "Unable to create slot"})
    return _response(201, {"slot": item})


def _get_slot(slot_id: str):
    result = _table("SLOTS_TABLE").get_item(Key={"slotId": slot_id}, ConsistentRead=True)
    item = result.get("Item")
    if not item:
        return _response(404, {"message": "Slot not found"})
    return _response(200, {"slot": item})


def _join_waitlist(event, user_id, role):
    if role != "patient":
        return _response(403, {"message": "Only patients can join a waitlist"})
    try:
        data = _body(event)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return _response(400, {"message": "Request body must be valid JSON"})
    slot_id = data.get("slotId")
    if not isinstance(slot_id, str) or not slot_id.strip():
        return _response(400, {"message": "slotId is required"})
    slot_id = slot_id.strip()
    try:
        slot_response = _table("SLOTS_TABLE").get_item(Key={"slotId": slot_id}, ConsistentRead=True)
        slot = slot_response.get("Item")
        if not slot or slot.get("status") != "OPEN":
            return _response(404, {"message": "Open slot not found"})
        joined_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        waitlist_key = f"W#{joined_at}#{user_id}"
        item = {
            "slotId": slot_id,
            "waitlistKey": waitlist_key,
            "patientId": user_id,
            "joinedAt": joined_at,
            "status": "WAITING",
            "type": "WAITLIST",
        }
        _table("WAITLIST_TABLE").put_item(
            Item=item,
            ConditionExpression=Attr("slotId").not_exists() & Attr("waitlistKey").not_exists(),
        )
        return _response(201, {"waitlistEntry": item})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _response(409, {"message": "Waitlist entry already exists"})
        logger.exception(json.dumps({"event": "waitlist_join_failed"}))
        return _response(500, {"message": "Unable to join waitlist"})
    except (BotoCoreError, RuntimeError):
        logger.exception(json.dumps({"event": "waitlist_join_failed"}))
        return _response(500, {"message": "Unable to join waitlist"})


def _my_waitlist(user_id):
    try:
        table = _table("WAITLIST_TABLE")
        result = table.query(
            IndexName="gsi-patient",
            KeyConditionExpression=Key("patientId").eq(user_id),
        )
        items = list(result.get("Items", []))
        while result.get("LastEvaluatedKey"):
            result = table.query(
                IndexName="gsi-patient",
                KeyConditionExpression=Key("patientId").eq(user_id),
                ExclusiveStartKey=result["LastEvaluatedKey"],
            )
            items.extend(result.get("Items", []))
        return _response(200, {"items": items, "count": len(items)})
    except (BotoCoreError, ClientError, RuntimeError):
        logger.exception(json.dumps({"event": "waitlist_list_failed"}))
        return _response(500, {"message": "Unable to retrieve waitlist"})


def _leave_waitlist(slot_id, user_id):
    table = _table("WAITLIST_TABLE")
    try:
        result = table.query(
            IndexName="gsi-patient",
            KeyConditionExpression=Key("patientId").eq(user_id) & Key("slotId").eq(slot_id),
        )
        removed = 0
        for item in result.get("Items", []):
            if item.get("type") == "WAITLIST" and item.get("status") == "WAITING":
                table.delete_item(Key={"slotId": item["slotId"], "waitlistKey": item["waitlistKey"]})
                removed += 1
        if not removed:
            return _response(404, {"message": "Active waitlist entry not found"})
        return _response(200, {"message": "Removed from waitlist", "removed": removed})
    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({"event": "waitlist_leave_failed"}))
        return _response(500, {"message": "Unable to leave waitlist"})


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None)
    method = str(event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    path = _path(event)
    user_id, role = _principal(event)
    logger.info(json.dumps({"event": "api_request", "method": method, "path": path, "requestId": request_id}))

    # The API Gateway authorizer protects the API. Check for context here too,
    # so direct Lambda invocation cannot accidentally bypass route-level checks.
    if path != "/health" and not user_id:
        return _response(401, {"message": "Unauthorized"})

    try:
        if method == "GET" and path == "/health":
            return _response(200, {"service": "medislot-api", "status": "ok"})
        if method == "GET" and path == "/slots":
            return _list_slots(event)
        if method == "POST" and path == "/slots":
            return _create_slot(event, user_id, role)
        match = re.fullmatch(r"/slots/([^/]+)", path)
        if method == "GET" and match:
            return _get_slot(match.group(1))
        if method == "POST" and path == "/waitlist":
            return _join_waitlist(event, user_id, role)
        if method == "GET" and path == "/waitlist":
            if role != "patient":
                return _response(403, {"message": "Only patients can view their waitlist"})
            return _my_waitlist(user_id)
        match = re.fullmatch(r"/waitlist/([^/]+)", path)
        if method == "DELETE" and match:
            if role != "patient":
                return _response(403, {"message": "Only patients can leave their waitlist"})
            return _leave_waitlist(match.group(1), user_id)
        return _response(404, {"message": "Route not found", "path": path, "method": method})
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _response(400, {"message": str(exc)})
    except (BotoCoreError, ClientError, RuntimeError):
        logger.exception(json.dumps({"event": "api_dependency_error", "requestId": request_id}))
        return _response(500, {"message": "Request could not be completed", "requestId": request_id})
