"""MediSlot application API router.

Handles doctor profiles, scheduling, slot discovery, appointment booking,
patient waitlists and related API operations.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

import base64
import json
import logging
import os
import re
import time
from datetime import (
    date,
    datetime,
    time as dt_time,
    timedelta,
    timezone,
)
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import pymysql
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

try:
    from .availability_rules import (
        validate_doctor_day,
        validate_exception_payload,
    )
except ImportError:
    from availability_rules import (
        validate_doctor_day,
        validate_exception_payload,
    )


IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DDB = None
_SSM = None


# ---------------------------------------------------------
# AWS CLIENTS
# ---------------------------------------------------------

def _dynamodb():
    global _DDB

    if _DDB is None:
        _DDB = boto3.resource("dynamodb")

    return _DDB


def _ssm():
    global _SSM

    if _SSM is None:
        _SSM = boto3.client("ssm")

    return _SSM


def _table(env_name: str):
    table_name = os.environ.get(env_name)

    if not table_name:
        raise RuntimeError(
            f"Missing required environment variable: {env_name}"
        )

    return _dynamodb().Table(table_name)


def _parameter(name: str, decrypt: bool = False):
    return _ssm().get_parameter(
        Name=name,
        WithDecryption=decrypt,
    )["Parameter"]["Value"]


# ---------------------------------------------------------
# RESPONSE AND REQUEST HELPERS
# ---------------------------------------------------------

def _json_default(value: Any):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)

    if isinstance(value, (date, datetime, dt_time)):
        return value.isoformat()

    if isinstance(value, timedelta):
        return str(value)

    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _response(status: int, body: dict[str, Any]):
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
            ),
            "X-Content-Type-Options": "nosniff",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")

    if raw in (None, ""):
        return {}

    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")

    if isinstance(raw, dict):
        return raw

    if not isinstance(raw, str):
        raise ValueError("Request body must be a JSON object")

    result = json.loads(raw)

    if not isinstance(result, dict):
        raise ValueError("Request body must be a JSON object")

    return result


def _principal(event: dict[str, Any]):
    authorizer = (
        (event.get("requestContext") or {}).get("authorizer") or {}
    )

    user_id = (
        authorizer.get("userId")
        or authorizer.get("userid")
    )

    role = authorizer.get("role")

    if user_id is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(user_id)):
            user_id = None

    return (
        str(user_id) if user_id else None,
        str(role).lower() if role else None,
    )


def _path(event: dict[str, Any]) -> str:
    path = event.get("path") or event.get("rawPath") or "/"

    path = (
        "/" + str(path).strip("/")
        if str(path).strip("/")
        else "/"
    )

    stage = os.environ.get("ENVIRONMENT", "dev")

    if path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]

    elif path == f"/{stage}":
        path = "/"

    if path != "/":
        path = path.rstrip("/")

    return path


def _query(event: dict[str, Any]):
    params = event.get("queryStringParameters") or {}

    return {
        str(key): str(value)
        for key, value in params.items()
        if value is not None
    }


def _iso_utc(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "startUtc must be an ISO-8601 timestamp with a timezone"
        )

    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )

    except ValueError as exc:
        raise ValueError(
            "startUtc must be a valid ISO-8601 timestamp"
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError(
            "startUtc must include a timezone, for example Z"
        )

    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _scan_all(table, **kwargs):
    items = []

    result = table.scan(**kwargs)
    items.extend(result.get("Items", []))

    while result.get("LastEvaluatedKey"):
        result = table.scan(
            ExclusiveStartKey=result["LastEvaluatedKey"],
            **kwargs,
        )

        items.extend(result.get("Items", []))

    return items


# ---------------------------------------------------------
# MYSQL CONNECTION
# ---------------------------------------------------------

def _db_connection():
    environment = os.environ.get("ENVIRONMENT", "dev")

    host_parameter = os.environ.get(
        "DB_HOST_PARAMETER",
        f"/medislot/{environment}/db/host",
    )

    port_parameter = os.environ.get(
        "DB_PORT_PARAMETER",
        f"/medislot/{environment}/db/port",
    )

    password_parameter = os.environ.get(
        "DB_PASSWORD_PARAMETER",
        f"/medislot/{environment}/db/app-password",
    )

    return pymysql.connect(
        host=_parameter(host_parameter),
        port=int(_parameter(port_parameter)),
        user=os.environ["DB_USER"],
        password=_parameter(password_parameter, True),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ---------------------------------------------------------
# SLOT MANAGEMENT
# ---------------------------------------------------------

def _list_slots(event):
    table = _table("SLOTS_TABLE")
    params = _query(event)

    doctor_id = params.get("doctorId")
    start_from = params.get("startFrom")
    start_to = params.get("startTo")

    expression = Attr("status").eq("OPEN")

    if doctor_id:
        key_expression = Key("doctorId").eq(doctor_id)

        if start_from and start_to:
            key_expression &= Key("startUtc").between(
                _iso_utc(start_from),
                _iso_utc(start_to),
            )

        elif start_from:
            key_expression &= Key("startUtc").gte(
                _iso_utc(start_from)
            )

        elif start_to:
            key_expression &= Key("startUtc").lte(
                _iso_utc(start_to)
            )

        result = table.query(
            IndexName="gsi-doctor-time",
            KeyConditionExpression=key_expression,
            FilterExpression=expression,
        )

        items = list(result.get("Items", []))

        while result.get("LastEvaluatedKey"):
            result = table.query(
                IndexName="gsi-doctor-time",
                KeyConditionExpression=key_expression,
                FilterExpression=expression,
                ExclusiveStartKey=result["LastEvaluatedKey"],
            )

            items.extend(result.get("Items", []))

    else:
        items = _scan_all(
            table,
            FilterExpression=expression,
        )

        if start_from:
            lower = _iso_utc(start_from)
            items = [
                item for item in items
                if item.get("startUtc", "") >= lower
            ]

        if start_to:
            upper = _iso_utc(start_to)
            items = [
                item for item in items
                if item.get("startUtc", "") <= upper
            ]

    items.sort(
        key=lambda item: (
            item.get("startUtc", ""),
            item.get("slotId", ""),
        )
    )

    return _response(
        200,
        {
            "items": items,
            "count": len(items),
        },
    )


def _create_slot(event, user_id, role):
    if role != "doctor":
        return _response(
            403,
            {"message": "Only doctors can create slots"},
        )

    data = _body(event)

    try:
        start_utc = _iso_utc(data.get("startUtc"))

        duration = data.get("durationMinutes", 15)
        capacity = data.get("capacity", 1)

        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 5 <= duration <= 180
        ):
            raise ValueError(
                "durationMinutes must be an integer between 5 and 180"
            )

        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= 50
        ):
            raise ValueError(
                "capacity must be an integer between 1 and 50"
            )

        if not data.get("consultationTypeId"):
            raise ValueError("consultationTypeId is required")

        raw_id = str(data["consultationTypeId"]).strip()

        consultation_id = (
            "CUSTOM"
            if raw_id.upper() == "CUSTOM"
            else str(int(raw_id))
        )

    except (ValueError, TypeError) as exc:
        return _response(400, {"message": str(exc)})

    if consultation_id != "CUSTOM":
        conn = _db_connection()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT duration_min, capacity
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                      AND doctor_user_id = %s
                      AND is_active = 1
                    """,
                    (consultation_id, user_id),
                )

                consultation = cur.fetchone()

            if not consultation:
                return _response(
                    400,
                    {
                        "message": (
                            "Consultation type does not belong to "
                            "this doctor or is inactive"
                        )
                    },
                )

            duration = int(consultation["duration_min"])
            capacity = int(consultation["capacity"])

        finally:
            conn.close()

    start_dt = datetime.fromisoformat(
        start_utc.replace("Z", "+00:00")
    )

    end_utc = (
        start_dt + timedelta(minutes=duration)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    slot_id = (
        f"{user_id}#{consultation_id}#{start_utc}"
    )

    now = int(datetime.now(timezone.utc).timestamp())

    item = {
        "slotId": slot_id,
        "doctorId": str(user_id),
        "consultationTypeId": consultation_id,
        "startUtc": start_utc,
        "endUtc": end_utc,
        "durationMinutes": duration,
        "capacity": capacity,
        "bookedCount": 0,
        "status": "OPEN",
        "source": "MANUAL",
        "expiresAt": now + 90 * 86400,
        "createdAt": now,
        "updatedAt": now,
    }

    try:
        _table("SLOTS_TABLE").put_item(
            Item=item,
            ConditionExpression=Attr("slotId").not_exists(),
        )

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return _response(
                409,
                {
                    "message": (
                        "A slot already exists for this doctor, "
                        "consultation type and start time"
                    )
                },
            )

        logger.exception("Slot creation failed")

        return _response(
            500,
            {"message": "Unable to create slot"},
        )

    except BotoCoreError:
        logger.exception("Slot creation failed")

        return _response(
            500,
            {"message": "Unable to create slot"},
        )

    return _response(201, {"slot": item})


def _get_slot(slot_id: str):
    result = _table("SLOTS_TABLE").get_item(
        Key={"slotId": slot_id},
        ConsistentRead=True,
    )

    item = result.get("Item")

    if not item:
        return _response(404, {"message": "Slot not found"})

    return _response(200, {"slot": item})


# ---------------------------------------------------------
# WAITLIST
# ---------------------------------------------------------

def _join_waitlist(event, user_id, role):
    if role != "patient":
        return _response(
            403,
            {"message": "Only patients can join a waitlist"},
        )

    data = _body(event)

    slot_id = data.get("slotId")

    if not isinstance(slot_id, str) or not slot_id.strip():
        return _response(400, {"message": "slotId is required"})

    slot_id = slot_id.strip()

    try:
        slot_response = _table("SLOTS_TABLE").get_item(
            Key={"slotId": slot_id},
            ConsistentRead=True,
        )

        slot = slot_response.get("Item")

        if not slot or slot.get("status") != "OPEN":
            return _response(
                404,
                {"message": "Open slot not found"},
            )

        joined_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

        waitlist_key = f"W#{joined_at}#{user_id}"

        item = {
            "slotId": slot_id,
            "waitlistKey": waitlist_key,
            "patientId": str(user_id),
            "joinedAt": joined_at,
            "status": "WAITING",
            "type": "WAITLIST",
        }

        _table("WAITLIST_TABLE").put_item(
            Item=item,
            ConditionExpression=(
                Attr("slotId").not_exists()
                & Attr("waitlistKey").not_exists()
            ),
        )

        return _response(
            201,
            {"waitlistEntry": item},
        )

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return _response(
                409,
                {"message": "Waitlist entry already exists"},
            )

        logger.exception("Waitlist join failed")

        return _response(
            500,
            {"message": "Unable to join waitlist"},
        )

    except (BotoCoreError, RuntimeError):
        logger.exception("Waitlist join failed")

        return _response(
            500,
            {"message": "Unable to join waitlist"},
        )


def _my_waitlist(user_id):
    try:
        table = _table("WAITLIST_TABLE")

        result = table.query(
            IndexName="gsi-patient",
            KeyConditionExpression=Key("patientId").eq(str(user_id)),
        )

        items = list(result.get("Items", []))

        while result.get("LastEvaluatedKey"):
            result = table.query(
                IndexName="gsi-patient",
                KeyConditionExpression=Key("patientId").eq(str(user_id)),
                ExclusiveStartKey=result["LastEvaluatedKey"],
            )

            items.extend(result.get("Items", []))

        return _response(
            200,
            {
                "items": items,
                "count": len(items),
            },
        )

    except (BotoCoreError, ClientError, RuntimeError):
        logger.exception("Waitlist retrieval failed")

        return _response(
            500,
            {"message": "Unable to retrieve waitlist"},
        )


def _leave_waitlist(slot_id, user_id):
    table = _table("WAITLIST_TABLE")

    try:
        result = table.query(
            IndexName="gsi-patient",
            KeyConditionExpression=(
                Key("patientId").eq(str(user_id))
                & Key("slotId").eq(slot_id)
            ),
        )

        removed = 0

        for item in result.get("Items", []):
            if (
                item.get("type") == "WAITLIST"
                and item.get("status") == "WAITING"
            ):
                table.delete_item(
                    Key={
                        "slotId": item["slotId"],
                        "waitlistKey": item["waitlistKey"],
                    }
                )

                removed += 1

        if not removed:
            return _response(
                404,
                {"message": "Active waitlist entry not found"},
            )

        return _response(
            200,
            {
                "message": "Removed from waitlist",
                "removed": removed,
            },
        )

    except (BotoCoreError, ClientError):
        logger.exception("Waitlist removal failed")

        return _response(
            500,
            {"message": "Unable to leave waitlist"},
        )
    # ---------------------------------------------------------
# REFERENCE DATA AND USER ACCOUNT
# ---------------------------------------------------------

def _reference_data(table_name):
    conn = _db_connection()

    try:
        with conn.cursor() as cur:
            if table_name == "specialisations":
                cur.execute(
                    """
                    SELECT specialisation_id, name
                    FROM specialisations
                    ORDER BY name
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT hospital_id, name, city, address
                    FROM hospitals
                    WHERE status = 'active'
                    ORDER BY city, name
                    """
                )

            return _response(200, {"items": cur.fetchall()})

    finally:
        conn.close()


def _my_account(user_id):
    conn = _db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, name, email, role, status,
                       created_at, updated_at
                FROM users
                WHERE user_id = %s
                """,
                (user_id,),
            )

            user = cur.fetchone()

            if not user:
                return _response(404, {"message": "User not found"})

            if user["role"] == "patient":
                cur.execute(
                    "SELECT * FROM patients WHERE user_id = %s",
                    (user_id,),
                )
                user["profile"] = cur.fetchone()

            elif user["role"] == "doctor":
                cur.execute(
                    "SELECT * FROM doctors WHERE user_id = %s",
                    (user_id,),
                )
                user["profile"] = cur.fetchone()

            return _response(200, {"user": user})

    finally:
        conn.close()


# ---------------------------------------------------------
# DOCTOR PROFILE
# ---------------------------------------------------------

def _doctor_profile(user_id, method, event):
    data = _body(event) if method in {"POST", "PUT", "PATCH"} else {}

    allowed = {
        "name",
        "licenseNumber",
        "specialisationId",
        "hospitalId",
        "yearsExperience",
        "qualifications",
        "languages",
        "timezone",
    }

    if set(data) - allowed:
        return _response(
            400,
            {
                "message": "Unsupported doctor profile fields",
                "allowedFields": sorted(allowed),
            },
        )

    if (
        "timezone" in data
        and data["timezone"] != "Asia/Kolkata"
    ):
        return _response(
            400,
            {
                "message": (
                    "MediSlot doctor schedules use Asia/Kolkata (IST)."
                )
            },
        )

    conn = _db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, name, email, status
                FROM users
                WHERE user_id = %s
                  AND role = 'doctor'
                """,
                (user_id,),
            )

            user = cur.fetchone()

            if not user:
                return _response(
                    404,
                    {"message": "Doctor account not found"},
                )

            cur.execute(
                "SELECT user_id FROM doctors WHERE user_id = %s",
                (user_id,),
            )

            exists = cur.fetchone() is not None

            if method == "GET":
                if not exists:
                    return _response(
                        404,
                        {"message": "Doctor profile not found"},
                    )

                cur.execute(
                    """
                    SELECT
                        u.user_id,
                        u.name,
                        u.email,
                        u.status,
                        d.license_number,
                        d.specialisation_id,
                        s.name AS specialisation,
                        d.hospital_id,
                        h.name AS hospital,
                        d.years_experience,
                        d.qualifications,
                        d.languages,
                        'Asia/Kolkata' AS timezone,
                        d.photo_key,
                        d.created_at,
                        d.updated_at
                    FROM users u
                    JOIN doctors d
                        ON d.user_id = u.user_id
                    LEFT JOIN specialisations s
                        ON s.specialisation_id = d.specialisation_id
                    LEFT JOIN hospitals h
                        ON h.hospital_id = d.hospital_id
                    WHERE u.user_id = %s
                      AND u.role = 'doctor'
                    """,
                    (user_id,),
                )

                profile = cur.fetchone()

                return _response(200, {"profile": profile})

            if method == "POST":
                if exists:
                    return _response(
                        409,
                        {
                            "message": (
                                "Doctor profile already exists; "
                                "use PUT to update it."
                            )
                        },
                    )

                required = ("licenseNumber", "specialisationId")

                if any(key not in data for key in required):
                    return _response(
                        400,
                        {
                            "message": (
                                "licenseNumber and specialisationId "
                                "are required"
                            )
                        },
                    )

                full_name = str(
                    data.get("name") or user["name"]
                ).strip()

                if not full_name or len(full_name) > 150:
                    return _response(
                        400,
                        {"message": "name must contain 1-150 characters"},
                    )

                cur.execute(
                    """
                    INSERT INTO doctors (
                        user_id,
                        full_name,
                        license_number,
                        specialisation_id,
                        hospital_id,
                        years_experience,
                        qualifications,
                        languages,
                        timezone
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Asia/Kolkata')
                    """,
                    (
                        user_id,
                        full_name,
                        str(data["licenseNumber"]).strip(),
                        int(data["specialisationId"]),
                        data.get("hospitalId") or None,
                        data.get("yearsExperience") or None,
                        data.get("qualifications") or None,
                        data.get("languages") or None,
                    ),
                )

                cur.execute(
                    """
                    UPDATE users
                    SET name = %s
                    WHERE user_id = %s
                      AND role = 'doctor'
                    """,
                    (full_name, user_id),
                )

                conn.commit()

                return _response(
                    201,
                    {
                        "message": "Doctor profile created",
                        "userId": int(user_id),
                        "timezone": "Asia/Kolkata",
                    },
                )

            if not exists:
                return _response(
                    404,
                    {
                        "message": (
                            "Doctor profile not found; "
                            "create it with POST /doctors/me"
                        )
                    },
                )

            if not data:
                return _response(
                    400,
                    {"message": "No profile fields supplied"},
                )

            if "name" in data:
                name = data["name"]

                if (
                    not isinstance(name, str)
                    or not name.strip()
                    or len(name.strip()) > 120
                ):
                    return _response(
                        400,
                        {"message": "name must contain 1-120 characters"},
                    )

                cur.execute(
                    """
                    UPDATE users
                    SET name = %s
                    WHERE user_id = %s
                      AND role = 'doctor'
                    """,
                    (name.strip(), user_id),
                )

                cur.execute(
                    """
                    UPDATE doctors
                    SET full_name = %s
                    WHERE user_id = %s
                    """,
                    (name.strip(), user_id),
                )

            update_statements = {
                "licenseNumber": (
                    "UPDATE doctors SET license_number=%s WHERE user_id=%s"
                ),
                "specialisationId": (
                    "UPDATE doctors SET specialisation_id=%s WHERE user_id=%s"
                ),
                "hospitalId": (
                    "UPDATE doctors SET hospital_id=%s WHERE user_id=%s"
                ),
                "yearsExperience": (
                    "UPDATE doctors SET years_experience=%s WHERE user_id=%s"
                ),
                "qualifications": (
                    "UPDATE doctors SET qualifications=%s WHERE user_id=%s"
                ),
                "languages": (
                    "UPDATE doctors SET languages=%s WHERE user_id=%s"
                ),
            }

            for api_name, statement in update_statements.items():
                if api_name not in data:
                    continue

                value = data[api_name]

                if api_name == "licenseNumber":
                    value = str(value).strip()

                    if not value or len(value) > 50:
                        raise ValueError(
                            "licenseNumber must contain 1-50 characters"
                        )

                elif api_name in {
                    "specialisationId",
                    "hospitalId",
                    "yearsExperience",
                }:
                    value = (
                        int(value)
                        if value not in (None, "")
                        else None
                    )

                    if (
                        api_name == "yearsExperience"
                        and value is not None
                        and not 0 <= value <= 80
                    ):
                        raise ValueError(
                            "yearsExperience must be between 0 and 80"
                        )

                    if (
                        api_name in {"specialisationId", "hospitalId"}
                        and value is not None
                        and value <= 0
                    ):
                        raise ValueError(
                            f"{api_name} must be a positive integer"
                        )

                if value == "":
                    value = None

                cur.execute(statement, (value, user_id))

            cur.execute(
                """
                UPDATE doctors
                SET timezone = 'Asia/Kolkata'
                WHERE user_id = %s
                """,
                (user_id,),
            )

        conn.commit()

        return _response(
            200,
            {
                "message": "Doctor profile updated",
                "timezone": "Asia/Kolkata",
            },
        )

    except pymysql.err.IntegrityError:
        conn.rollback()

        return _response(
            409,
            {
                "message": (
                    "A license number, specialisation or hospital "
                    "reference conflicts with an existing record"
                )
            },
        )

    except (KeyError, TypeError, ValueError) as exc:
        conn.rollback()
        return _response(400, {"message": str(exc)})

    finally:
        conn.close()


# ---------------------------------------------------------
# DOCTOR PHOTO UPLOAD AND RETRIEVAL
# ---------------------------------------------------------

def _presign_photo(user_id, event):
    data = _body(event)
    content_type = data.get("contentType")

    extensions = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }

    if content_type not in extensions:
        return _response(
            400,
            {
                "message": (
                    "contentType must be image/jpeg, image/png or image/webp"
                )
            },
        )

    bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")

    if not bucket:
        return _response(
            500,
            {"message": "Photo storage is not configured"},
        )

    # Numeric timestamp key: no UUIDs.
    key = (
        f"doctors/{int(user_id)}/"
        f"profile-{time.time_ns()}.{extensions[content_type]}"
    )

    s3 = boto3.client("s3")

    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=300,
        )

        conn = _db_connection()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE doctors
                    SET photo_key = %s
                    WHERE user_id = %s
                    """,
                    (key, user_id),
                )

                if cur.rowcount == 0:
                    conn.rollback()

                    return _response(
                        404,
                        {"message": "Doctor profile not found"},
                    )

            conn.commit()

        finally:
            conn.close()

        return _response(
            200,
            {
                "message": "Photo upload URL created",
                "uploadUrl": upload_url,
                "photoKey": key,
                "expiresIn": 300,
            },
        )

    except Exception:
        logger.exception("Doctor photo upload URL creation failed")

        return _response(
            500,
            {"message": "Could not prepare photo upload"},
        )


def _doctor_photo_url(user_id):
    bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")

    if not bucket:
        return _response(
            500,
            {"message": "Photo storage is not configured"},
        )

    conn = _db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT photo_key
                FROM doctors
                WHERE user_id = %s
                """,
                (user_id,),
            )

            doctor = cur.fetchone()

        if not doctor:
            return _response(
                404,
                {"message": "Doctor profile not found"},
            )

        photo_key = doctor.get("photo_key")

    finally:
        conn.close()

    if not photo_key:
        return _response(200, {"photoUrl": None})

    try:
        s3 = boto3.client("s3")

        photo_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": photo_key,
            },
            ExpiresIn=900,
        )

        return _response(
            200,
            {
                "photoUrl": photo_url,
                "expiresIn": 900,
            },
        )

    except Exception:
        logger.exception("Doctor photo URL generation failed")

        return _response(
            500,
            {"message": "Could not retrieve doctor photo"},
        )


# ---------------------------------------------------------
# DOCTOR SCHEDULE VALIDATION
# ---------------------------------------------------------

def _validate_doctor_schedule(cursor, user_id):
    cursor.execute(
        """
        SELECT consultation_type_id, is_active
        FROM consultation_types
        WHERE doctor_user_id = %s
        """,
        (user_id,),
    )

    types = cursor.fetchall()
    schedules = []

    for item in types:
        consultation_id = item["consultation_type_id"]

        cursor.execute(
            """
            SELECT day_of_week, start_time, end_time
            FROM availability_patterns
            WHERE doctor_user_id = %s
              AND consultation_type_id = %s
            """,
            (user_id, consultation_id),
        )

        patterns = cursor.fetchall()

        cursor.execute(
            """
            SELECT exception_date, exception_type, start_time, end_time
            FROM availability_exceptions
            WHERE doctor_user_id = %s
              AND consultation_type_id = %s
            """,
            (user_id, consultation_id),
        )

        exceptions = cursor.fetchall()

        cursor.execute(
            """
            SELECT blackout_date
            FROM blackout_dates
            WHERE doctor_user_id = %s
            """,
            (user_id,),
        )

        blackouts = [
            row["blackout_date"]
            for row in cursor.fetchall()
        ]

        schedules.append(
            {
                "user_id": str(user_id),
                "consultation_type_id": int(consultation_id),
                "is_active": bool(item["is_active"]),
                "patterns": patterns,
                "exceptions": exceptions,
                "blackouts": blackouts,
            }
        )

    first_day = datetime.now(IST).date()

    dates_to_validate = {
        first_day + timedelta(days=offset)
        for offset in range(30)
    }

    for schedule in schedules:
        dates_to_validate.update(
            date.fromisoformat(str(row["exception_date"])[:10])
            for row in schedule["exceptions"]
        )

    for target_day in sorted(dates_to_validate):
        validate_doctor_day(schedules, target_day)


# ---------------------------------------------------------
# DOCTOR CONSULTATION TYPES, AVAILABILITY AND BLACKOUTS
# ---------------------------------------------------------

def _doctor_collection(
    table_name,
    user_id,
    method,
    event,
    resource_id=None,
):
    data = (
        _body(event)
        if method in {"POST", "PUT", "PATCH"}
        else {}
    )

    conn = _db_connection()

    try:
        with conn.cursor() as cur:

            # CONSULTATION TYPES
            if table_name == "consultation_types":

                if method == "GET":
                    cur.execute(
                        """
                        SELECT *
                        FROM consultation_types
                        WHERE doctor_user_id = %s
                        ORDER BY consultation_type_id
                        """,
                        (user_id,),
                    )

                    return _response(
                        200,
                        {"items": cur.fetchall()},
                    )

                if method == "POST":
                    required = (
                        "name",
                        "durationMin",
                        "feeAmount",
                        "capacity",
                    )

                    if any(key not in data for key in required):
                        return _response(
                            400,
                            {
                                "message": (
                                    "name, durationMin, feeAmount "
                                    "and capacity are required"
                                )
                            },
                        )

                    name = str(data["name"]).strip().lower()
                    duration = int(data["durationMin"])
                    fee = Decimal(str(data["feeAmount"]))
                    capacity = int(data["capacity"])

                    if not name or len(name) > 40:
                        raise ValueError(
                            "name must contain 1-40 characters"
                        )

                    if not 5 <= duration <= 180:
                        raise ValueError(
                            "durationMin must be between 5 and 180"
                        )

                    if fee < 0 or fee > Decimal("99999999.99"):
                        raise ValueError(
                            "feeAmount is outside the supported range"
                        )

                    if not 1 <= capacity <= 50:
                        raise ValueError(
                            "capacity must be between 1 and 50"
                        )

                    cur.execute(
                        """
                        INSERT INTO consultation_types (
                            doctor_user_id,
                            name,
                            duration_min,
                            fee_amount,
                            capacity
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            name,
                            duration,
                            fee,
                            capacity,
                        ),
                    )

                    new_id = int(cur.lastrowid)
                    conn.commit()

                    return _response(
                        201,
                        {
                            "consultationTypeId": new_id,
                            "message": "Consultation type created",
                        },
                    )

                if method in {"PUT", "PATCH"} and resource_id:
                    fields = {
                        "name",
                        "durationMin",
                        "feeAmount",
                        "capacity",
                        "isActive",
                    }

                    if not data or set(data) - fields:
                        return _response(
                            400,
                            {
                                "message": (
                                    "Unsupported or empty consultation "
                                    "type update"
                                ),
                                "allowedFields": sorted(fields),
                            },
                        )

                    cur.execute(
                        """
                        SELECT consultation_type_id
                        FROM consultation_types
                        WHERE consultation_type_id = %s
                          AND doctor_user_id = %s
                        """,
                        (resource_id, user_id),
                    )

                    if not cur.fetchone():
                        return _response(
                            404,
                            {"message": "Consultation type not found"},
                        )

                    statements = {
                        "name": (
                            "UPDATE consultation_types SET name=%s "
                            "WHERE consultation_type_id=%s "
                            "AND doctor_user_id=%s"
                        ),
                        "durationMin": (
                            "UPDATE consultation_types SET duration_min=%s "
                            "WHERE consultation_type_id=%s "
                            "AND doctor_user_id=%s"
                        ),
                        "feeAmount": (
                            "UPDATE consultation_types SET fee_amount=%s "
                            "WHERE consultation_type_id=%s "
                            "AND doctor_user_id=%s"
                        ),
                        "capacity": (
                            "UPDATE consultation_types SET capacity=%s "
                            "WHERE consultation_type_id=%s "
                            "AND doctor_user_id=%s"
                        ),
                        "isActive": (
                            "UPDATE consultation_types SET is_active=%s "
                            "WHERE consultation_type_id=%s "
                            "AND doctor_user_id=%s"
                        ),
                    }

                    values = {}

                    if "name" in data:
                        value = str(data["name"]).strip().lower()

                        if not value or len(value) > 40:
                            raise ValueError(
                                "name must contain 1-40 characters"
                            )

                        values["name"] = value

                    if "durationMin" in data:
                        value = int(data["durationMin"])

                        if not 5 <= value <= 180:
                            raise ValueError(
                                "durationMin must be between 5 and 180"
                            )

                        values["durationMin"] = value

                    if "feeAmount" in data:
                        value = Decimal(str(data["feeAmount"]))

                        if value < 0:
                            raise ValueError(
                                "feeAmount must be non-negative"
                            )

                        values["feeAmount"] = value

                    if "capacity" in data:
                        value = int(data["capacity"])

                        if not 1 <= value <= 50:
                            raise ValueError(
                                "capacity must be between 1 and 50"
                            )

                        values["capacity"] = value

                    if "isActive" in data:
                        if not isinstance(data["isActive"], bool):
                            raise ValueError(
                                "isActive must be boolean"
                            )

                        values["isActive"] = int(data["isActive"])

                    for key, value in values.items():
                        cur.execute(
                            statements[key],
                            (value, resource_id, user_id),
                        )

                    _validate_doctor_schedule(cur, user_id)
                    conn.commit()

                    return _response(
                        200,
                        {"message": "Consultation type updated"},
                    )

                if method == "DELETE" and resource_id:
                    cur.execute(
                        """
                        UPDATE consultation_types
                        SET is_active = 0
                        WHERE consultation_type_id = %s
                          AND doctor_user_id = %s
                        """,
                        (resource_id, user_id),
                    )

                    if not cur.rowcount:
                        return _response(
                            404,
                            {"message": "Consultation type not found"},
                        )

                    conn.commit()

                    return _response(
                        200,
                        {"message": "Consultation type deactivated"},
                    )

            # WEEKLY AVAILABILITY PATTERNS
            elif table_name == "availability_patterns":

                if method == "GET":
                    cur.execute(
                        """
                        SELECT *
                        FROM availability_patterns
                        WHERE doctor_user_id = %s
                        ORDER BY day_of_week, start_time
                        """,
                        (user_id,),
                    )

                    return _response(
                        200,
                        {
                            "items": cur.fetchall(),
                            "timezone": "Asia/Kolkata",
                        },
                    )

                if method in {"POST", "PUT", "PATCH"}:
                    required = (
                        "consultationTypeId",
                        "dayOfWeek",
                        "startTime",
                        "endTime",
                    )

                    if any(key not in data for key in required):
                        raise ValueError(
                            "consultationTypeId, dayOfWeek, "
                            "startTime and endTime are required"
                        )

                    day = int(data["dayOfWeek"])
                    start_time = dt_time.fromisoformat(
                        str(data["startTime"])
                    )
                    end_time = dt_time.fromisoformat(
                        str(data["endTime"])
                    )

                    if not 1 <= day <= 7 or start_time >= end_time:
                        raise ValueError(
                            "Invalid weekday or time range"
                        )

                    consultation_id = int(data["consultationTypeId"])

                    cur.execute(
                        """
                        SELECT consultation_type_id
                        FROM consultation_types
                        WHERE consultation_type_id = %s
                          AND doctor_user_id = %s
                          AND is_active = 1
                        """,
                        (consultation_id, user_id),
                    )

                    if not cur.fetchone():
                        return _response(
                            400,
                            {
                                "message": (
                                    "Active consultation type does not "
                                    "belong to this doctor"
                                )
                            },
                        )

                    if method == "POST":
                        cur.execute(
                            """
                            INSERT INTO availability_patterns (
                                doctor_user_id,
                                consultation_type_id,
                                day_of_week,
                                start_time,
                                end_time
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                user_id,
                                consultation_id,
                                day,
                                start_time,
                                end_time,
                            ),
                        )

                        new_id = int(cur.lastrowid)

                    else:
                        if not resource_id:
                            return _response(
                                400,
                                {"message": "patternId is required"},
                            )

                        cur.execute(
                            """
                            UPDATE availability_patterns
                            SET consultation_type_id = %s,
                                day_of_week = %s,
                                start_time = %s,
                                end_time = %s
                            WHERE pattern_id = %s
                              AND doctor_user_id = %s
                            """,
                            (
                                consultation_id,
                                day,
                                start_time,
                                end_time,
                                resource_id,
                                user_id,
                            ),
                        )

                        if not cur.rowcount:
                            return _response(
                                404,
                                {
                                    "message": (
                                        "Availability pattern not found"
                                    )
                                },
                            )

                        new_id = resource_id

                    _validate_doctor_schedule(cur, user_id)
                    conn.commit()

                    return _response(
                        201 if method == "POST" else 200,
                        {
                            "patternId": new_id,
                            "timezone": "Asia/Kolkata",
                        },
                    )

                if method == "DELETE" and resource_id:
                    cur.execute(
                        """
                        DELETE FROM availability_patterns
                        WHERE pattern_id = %s
                          AND doctor_user_id = %s
                        """,
                        (resource_id, user_id),
                    )

                    if not cur.rowcount:
                        return _response(
                            404,
                            {
                                "message": (
                                    "Availability pattern not found"
                                )
                            },
                        )

                    conn.commit()

                    return _response(
                        200,
                        {"message": "Availability pattern deleted"},
                    )

            # BLACKOUT DATES
            elif table_name == "blackout_dates":

                if method == "GET":
                    cur.execute(
                        """
                        SELECT *
                        FROM blackout_dates
                        WHERE doctor_user_id = %s
                        ORDER BY blackout_date
                        """,
                        (user_id,),
                    )

                    return _response(
                        200,
                        {
                            "items": cur.fetchall(),
                            "timezone": "Asia/Kolkata",
                        },
                    )

                if method == "POST":
                    day = date.fromisoformat(str(data["date"]))

                    if day < datetime.now(IST).date():
                        raise ValueError(
                            "Blackout date cannot be in the past"
                        )

                    cur.execute(
                        """
                        INSERT INTO blackout_dates (
                            doctor_user_id,
                            blackout_date,
                            reason
                        )
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, day, data.get("reason")),
                    )

                    new_id = int(cur.lastrowid)
                    conn.commit()

                    return _response(
                        201,
                        {
                            "blackoutId": new_id,
                            "date": day.isoformat(),
                        },
                    )

                if method in {"PUT", "PATCH"} and resource_id:
                    day = date.fromisoformat(str(data["date"]))

                    if day < datetime.now(IST).date():
                        raise ValueError(
                            "Blackout date cannot be in the past"
                        )

                    cur.execute(
                        """
                        UPDATE blackout_dates
                        SET blackout_date = %s,
                            reason = %s
                        WHERE blackout_id = %s
                          AND doctor_user_id = %s
                        """,
                        (
                            day,
                            data.get("reason"),
                            resource_id,
                            user_id,
                        ),
                    )

                    if not cur.rowcount:
                        return _response(
                            404,
                            {"message": "Blackout date not found"},
                        )

                    conn.commit()

                    return _response(
                        200,
                        {"message": "Blackout date updated"},
                    )

                if method == "DELETE" and resource_id:
                    cur.execute(
                        """
                        DELETE FROM blackout_dates
                        WHERE blackout_id = %s
                          AND doctor_user_id = %s
                        """,
                        (resource_id, user_id),
                    )

                    if not cur.rowcount:
                        return _response(
                            404,
                            {"message": "Blackout date not found"},
                        )

                    conn.commit()

                    return _response(
                        200,
                        {"message": "Blackout date deleted"},
                    )

            # DATE-SPECIFIC AVAILABILITY EXCEPTIONS
            elif table_name == "availability_exceptions":

                if method == "GET":
                    cur.execute(
                        """
                        SELECT *
                        FROM availability_exceptions
                        WHERE doctor_user_id = %s
                        ORDER BY exception_date, start_time
                        """,
                        (user_id,),
                    )

                    return _response(
                        200,
                        {
                            "items": cur.fetchall(),
                            "timezone": "Asia/Kolkata",
                        },
                    )

                if method in {"POST", "PUT", "PATCH"}:
                    required = (
                        "consultationTypeId",
                        "date",
                        "type",
                    )

                    if any(key not in data for key in required):
                        raise ValueError(
                            "consultationTypeId, date and type are required"
                        )

                    consultation_id = int(data["consultationTypeId"])
                    day = date.fromisoformat(str(data["date"]))

                    kind, start_time, end_time = (
                        validate_exception_payload(
                            data["type"],
                            data.get("startTime"),
                            data.get("endTime"),
                        )
                    )

                    cur.execute(
                        """
                        SELECT consultation_type_id
                        FROM consultation_types
                        WHERE consultation_type_id = %s
                          AND doctor_user_id = %s
                          AND is_active = 1
                        """,
                        (consultation_id, user_id),
                    )

                    if not cur.fetchone():
                        return _response(
                            400,
                            {
                                "message": (
                                    "Active consultation type does not "
                                    "belong to this doctor"
                                )
                            },
                        )

                    if method == "POST":
                        cur.execute(
                            """
                            INSERT INTO availability_exceptions (
                                doctor_user_id,
                                consultation_type_id,
                                exception_date,
                                exception_type,
                                start_time,
                                end_time
                            )
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                user_id,
                                consultation_id,
                                day,
                                kind,
                                start_time,
                                end_time,
                            ),
                        )

                        new_id = int(cur.lastrowid)

                    else:
                        cur.execute(
                            """
                            UPDATE availability_exceptions
                            SET consultation_type_id = %s,
                                exception_date = %s,
                                exception_type = %s,
                                start_time = %s,
                                end_time = %s
                            WHERE exception_id = %s
                              AND doctor_user_id = %s
                            """,
                            (
                                consultation_id,
                                day,
                                kind,
                                start_time,
                                end_time,
                                resource_id,
                                user_id,
                            ),
                        )

                        if not cur.rowcount:
                            return _response(
                                404,
                                {
                                    "message": (
                                        "Availability exception not found"
                                    )
                                },
                            )

                        new_id = resource_id

                    _validate_doctor_schedule(cur, user_id)
                    conn.commit()

                    return _response(
                        201 if method == "POST" else 200,
                        {
                            "exceptionId": new_id,
                            "timezone": "Asia/Kolkata",
                        },
                    )

                if method == "DELETE" and resource_id:
                    cur.execute(
                        """
                        DELETE FROM availability_exceptions
                        WHERE exception_id = %s
                          AND doctor_user_id = %s
                        """,
                        (resource_id, user_id),
                    )

                    if not cur.rowcount:
                        return _response(
                            404,
                            {
                                "message": (
                                    "Availability exception not found"
                                )
                            },
                        )

                    conn.commit()

                    return _response(
                        200,
                        {
                            "message": (
                                "Availability exception deleted"
                            )
                        },
                    )

        return _response(
            405,
            {"message": "Method not supported for this resource"},
        )

    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        conn.rollback()

        return _response(
            400,
            {"message": f"Invalid request: {exc}"},
        )

    except pymysql.err.IntegrityError:
        conn.rollback()

        return _response(
            409,
            {"message": "Duplicate or invalid scheduling record"},
        )

    except Exception:
        conn.rollback()
        logger.exception("Doctor scheduling operation failed")
        raise

    finally:
        conn.close()


# ---------------------------------------------------------
# DOCTOR DAILY SCHEDULE
# ---------------------------------------------------------

def _doctor_day_schedule(user_id, event):
    params = _query(event)
    day_value = params.get("date")

    if not day_value:
        return _response(
            400,
            {
                "message": (
                    "date query parameter is required (YYYY-MM-DD)"
                )
            },
        )

    try:
        local_day = date.fromisoformat(day_value)

    except ValueError:
        return _response(
            400,
            {"message": "date must be YYYY-MM-DD"},
        )

    local_start = datetime.combine(
        local_day,
        dt_time.min,
        tzinfo=IST,
    )

    local_end = datetime.combine(
        local_day + timedelta(days=1),
        dt_time.min,
        tzinfo=IST,
    )

    start_utc = (
        local_start.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    end_utc = (
        local_end.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    table = _table("SLOTS_TABLE")

    query_args = {
        "IndexName": "gsi-doctor-time",
        "KeyConditionExpression": (
            Key("doctorId").eq(str(user_id))
            & Key("startUtc").between(start_utc, end_utc)
        ),
    }

    items = []

    result = table.query(**query_args)
    items.extend(result.get("Items", []))

    while result.get("LastEvaluatedKey"):
        result = table.query(
            **query_args,
            ExclusiveStartKey=result["LastEvaluatedKey"],
        )

        items.extend(result.get("Items", []))

    # Exclude next-day midnight because the upper bound is inclusive.
    items = [
        item for item in items
        if item.get("localDate") == local_day.isoformat()
    ]

    items.sort(
        key=lambda item: (
            item.get("startUtc", ""),
            item.get("consultationTypeId", ""),
        )
    )

    return _response(
        200,
        {
            "date": local_day.isoformat(),
            "timezone": "Asia/Kolkata",
            "items": items,
            "count": len(items),
        },
    )


# ---------------------------------------------------------
# MATERIALIZER DISPATCH
# ---------------------------------------------------------

def _dispatch_materializer(
    doctor_id=None,
    date_from=None,
    date_to=None,
):
    logger.info(
        json.dumps(
            {
                "event": "materializer_dispatch_skipped",
                "doctorId": (
                    str(doctor_id) if doctor_id else None
                ),
                "reason": "scheduled_materialization_enabled",
            }
        )
    )

    return False


def _materializer_trigger(user_id=None):
    try:
        _dispatch_materializer(user_id)

    except Exception:
        logger.exception(
            "Could not enqueue materializer invocation"
        )
        raise


# ---------------------------------------------------------
# DOCTOR APPOINTMENTS
# ---------------------------------------------------------

def _doctor_appointments(user_id, event):
    params = _query(event)

    requested_status = params.get("status")
    requested_date = params.get("date")

    allowed_statuses = {
        "booked",
        "confirmed",
        "completed",
        "cancelled",
        "rescheduled",
        "no_show",
    }

    if (
        requested_status
        and requested_status not in allowed_statuses
    ):
        return _response(
            400,
            {
                "message": "Invalid appointment status",
                "allowedStatuses": sorted(allowed_statuses),
            },
        )

    if requested_date:
        try:
            date.fromisoformat(requested_date)

        except ValueError:
            return _response(
                400,
                {"message": "date must be YYYY-MM-DD"},
            )

    sql = """
        SELECT
            a.appointment_id,
            a.patient_user_id,
            a.doctor_user_id,
            a.consultation_type_id,
            a.slot_id,
            a.slot_start_utc,
            a.slot_end_utc,
            a.status,
            a.active_flag,
            a.fee_amount,

            u.name AS patient_name,
            u.email AS patient_email,

            p.phone AS patient_phone,
            p.date_of_birth AS patient_date_of_birth,
            p.gender AS patient_gender,
            p.city AS patient_city,

            ct.name AS consultation_type,
            ct.duration_min AS consultation_duration

        FROM appointments a

        INNER JOIN users u
            ON u.user_id = a.patient_user_id

        LEFT JOIN patients p
            ON p.user_id = a.patient_user_id

        LEFT JOIN consultation_types ct
            ON ct.consultation_type_id = a.consultation_type_id

        WHERE a.doctor_user_id = %s
    """

    values = [int(user_id)]

    if requested_status:
        sql += " AND a.status = %s"
        values.append(requested_status)

    if requested_date:
        sql += " AND DATE(a.slot_start_utc) = %s"
        values.append(requested_date)

    sql += " ORDER BY a.slot_start_utc DESC LIMIT 200"

    conn = _db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(values))
            appointments = cur.fetchall()

        return _response(
            200,
            {
                "items": appointments,
                "count": len(appointments),
            },
        )

    finally:
        conn.close()


# ---------------------------------------------------------
# PATIENT APPOINTMENT BOOKING
# ---------------------------------------------------------

def _book_appointment(event, user_id, role):
    if role != "patient":
        return _response(
            403,
            {"message": "Only patients can book appointments"},
        )

    data = _body(event)
    slot_id = data.get("slotId")

    if not isinstance(slot_id, str) or not slot_id.strip():
        return _response(
            400,
            {"message": "slotId is required"},
        )

    slot_id = slot_id.strip()
    slots = _table("SLOTS_TABLE")

    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    try:
        updated = slots.update_item(
            Key={"slotId": slot_id},
            UpdateExpression=(
                "SET bookedCount = bookedCount + :one, "
                "updatedAt = :now"
            ),
            ConditionExpression=(
                "attribute_exists(slotId) "
                "AND #st = :open "
                "AND bookedCount < capacity "
                "AND startUtc > :now"
            ),
            ExpressionAttributeNames={
                "#st": "status",
            },
            ExpressionAttributeValues={
                ":one": 1,
                ":now": now,
                ":open": "OPEN",
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return _response(
                409,
                {
                    "message": (
                        "Slot is unavailable, expired or fully booked"
                    )
                },
            )

        logger.exception("Slot capacity update failed")

        return _response(
            500,
            {"message": "Unable to reserve slot"},
        )

    except BotoCoreError:
        logger.exception("Slot capacity update failed")

        return _response(
            500,
            {"message": "Unable to reserve slot"},
        )

    conn = None

    try:
        conn = _db_connection()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fee_amount
                FROM consultation_types
                WHERE consultation_type_id = %s
                  AND doctor_user_id = %s
                  AND is_active = 1
                """,
                (
                    int(updated["consultationTypeId"]),
                    str(updated["doctorId"]),
                ),
            )

            consultation = cur.fetchone()

            if not consultation:
                raise ValueError(
                    "Consultation type is no longer active"
                )

            start = datetime.fromisoformat(
                updated["startUtc"].replace("Z", "+00:00")
            ).replace(tzinfo=None)

            end = datetime.fromisoformat(
                updated["endUtc"].replace("Z", "+00:00")
            ).replace(tzinfo=None)

            cur.execute(
                """
                INSERT INTO appointments (
                    patient_user_id,
                    doctor_user_id,
                    consultation_type_id,
                    slot_id,
                    slot_start_utc,
                    slot_end_utc,
                    status,
                    active_flag,
                    fee_amount
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'booked', 1, %s
                )
                """,
                (
                    int(user_id),
                    int(updated["doctorId"]),
                    int(updated["consultationTypeId"]),
                    slot_id,
                    start,
                    end,
                    consultation["fee_amount"],
                ),
            )

            appointment_id = int(cur.lastrowid)

        conn.commit()

        return _response(
            201,
            {
                "message": "Appointment booked successfully",
                "appointmentId": appointment_id,
                "slotId": slot_id,
            },
        )

    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        # Release the reserved capacity if SQL persistence failed.
        try:
            slots.update_item(
                Key={"slotId": slot_id},
                UpdateExpression=(
                    "SET bookedCount = bookedCount - :one, "
                    "#st = :open, updatedAt = :now"
                ),
                ConditionExpression="bookedCount >= :one",
                ExpressionAttributeNames={
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":now": now,
                    ":open": "OPEN",
                },
            )

        except Exception:
            logger.exception(
                "Booking compensation failed; reconcile slot capacity"
            )

        logger.exception("Appointment persistence failed")

        return _response(
            409,
            {
                "message": (
                    "Appointment could not be confirmed; "
                    "slot reservation was released when possible"
                )
            },
        )

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------
# MAIN LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None)

    request_context = event.get("requestContext") or {}

    method = str(
        event.get("httpMethod")
        or (request_context.get("http") or {}).get("method")
        or "GET"
    ).upper()

    path = _path(event)
    user_id, role = _principal(event)

    logger.info(
        json.dumps(
            {
                "event": "api_request",
                "method": method,
                "path": path,
                "requestId": request_id,
                "userId": user_id,
                "role": role,
            }
        )
    )

    if method == "OPTIONS":
        return _response(200, {"message": "OK"})

    if path != "/health" and not user_id:
        return _response(
            401,
            {"message": "Unauthorized"},
        )

    try:
        # Health check
        if method == "GET" and path == "/health":
            return _response(
                200,
                {
                    "service": "medislot-api",
                    "status": "ok",
                },
            )

        # Reference data
        if (
            method == "GET"
            and path == "/reference/specialisations"
        ):
            return _reference_data("specialisations")

        if (
            method == "GET"
            and path == "/reference/hospitals"
        ):
            return _reference_data("hospitals")

        # Authenticated account
        if method == "GET" and path == "/users/me":
            return _my_account(user_id)

        # Doctor profile
        if (
            path == "/doctors/me"
            and role == "doctor"
            and method in {"GET", "POST", "PUT", "PATCH"}
        ):
            return _doctor_profile(user_id, method, event)

        # Doctor photo upload
        if (
            path == "/doctors/me/photo-upload"
            and role == "doctor"
            and method == "POST"
        ):
            return _presign_photo(user_id, event)

        # Doctor photo display URL
        if (
            path == "/doctors/me/photo-url"
            and role == "doctor"
            and method == "GET"
        ):
            return _doctor_photo_url(user_id)

        # Doctor appointments
        if (
            path == "/doctors/me/appointments"
            and role == "doctor"
            and method == "GET"
        ):
            return _doctor_appointments(user_id, event)

        # Doctor scheduling collections
        routes = [
            (
                "/doctors/me/consultation-types",
                "consultation_types",
            ),
            (
                "/doctors/me/availability-patterns",
                "availability_patterns",
            ),
            (
                "/doctors/me/blackout-dates",
                "blackout_dates",
            ),
            (
                "/doctors/me/availability-exceptions",
                "availability_exceptions",
            ),
        ]

        for route, table_name in routes:
            if path == route and role == "doctor":
                return _doctor_collection(
                    table_name,
                    user_id,
                    method,
                    event,
                )

            match = re.fullmatch(
                re.escape(route) + r"/([1-9][0-9]*)",
                path,
            )

            if match and role == "doctor":
                return _doctor_collection(
                    table_name,
                    user_id,
                    method,
                    event,
                    int(match.group(1)),
                )

        # Doctor daily schedule
        if (
            path == "/doctors/me/schedule"
            and method == "GET"
            and role == "doctor"
        ):
            return _doctor_day_schedule(user_id, event)

        # Slot operations
        if method == "GET" and path == "/slots":
            return _list_slots(event)

        if method == "POST" and path == "/slots":
            return _create_slot(event, user_id, role)

        match = re.fullmatch(r"/slots/([^/]+)", path)

        if method == "GET" and match:
            return _get_slot(match.group(1))

        # Patient appointment booking
        if method == "POST" and path == "/appointments":
            return _book_appointment(event, user_id, role)

        # Patient waitlist
        if method == "POST" and path == "/waitlist":
            return _join_waitlist(event, user_id, role)

        if method == "GET" and path == "/waitlist":
            if role != "patient":
                return _response(
                    403,
                    {
                        "message": (
                            "Only patients can view their waitlist"
                        )
                    },
                )

            return _my_waitlist(user_id)

        match = re.fullmatch(r"/waitlist/([^/]+)", path)

        if method == "DELETE" and match:
            if role != "patient":
                return _response(
                    403,
                    {
                        "message": (
                            "Only patients can leave their waitlist"
                        )
                    },
                )

            return _leave_waitlist(
                match.group(1),
                user_id,
            )

        return _response(
            404,
            {
                "message": "Route not found",
                "path": path,
                "method": method,
            },
        )

    except (
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        return _response(
            400,
            {"message": str(exc)},
        )

    except (
        BotoCoreError,
        ClientError,
        pymysql.MySQLError,
        RuntimeError,
    ):
        logger.exception(
            json.dumps(
                {
                    "event": "api_dependency_error",
                    "requestId": request_id,
                }
            )
        )

        return _response(
            500,
            {
                "message": "Request could not be completed",
                "requestId": request_id,
            },
        )

    except Exception:
        logger.exception(
            json.dumps(
                {
                    "event": "api_unexpected_error",
                    "requestId": request_id,
                }
            )
        )

        return _response(
            500,
            {
                "message": "Internal server error",
                "requestId": request_id,
            },
        )