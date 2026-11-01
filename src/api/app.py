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
import hashlib
import boto3
import pymysql
from zoneinfo import ZoneInfo
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DDB = None
_SSM = None
_S3 = None
_LAMBDA = None


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
    if isinstance(value, (date, datetime, dt_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status: int, body: dict[str, Any]):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,Idempotency-Key,X-Correlation-Id",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
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
        if not data.get("consultationTypeId"):
            raise ValueError("consultationTypeId is required")
        consultation_id = str(int(data["consultationTypeId"]))
    except ValueError as exc:
        return _response(400, {"message": str(exc)})
    if consultation_id != "CUSTOM":
        conn=_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT duration_min,capacity FROM consultation_types WHERE consultation_type_id=%s AND doctor_user_id=%s AND is_active=1",
                            (consultation_id,user_id))
                consultation=cur.fetchone()
            if not consultation: return _response(400,{"message":"Consultation type does not belong to this doctor or is inactive"})
            duration=int(consultation["duration_min"]); capacity=int(consultation["capacity"])
        finally: conn.close()
    start_dt=datetime.fromisoformat(start_utc.replace("Z","+00:00"))
    end_utc=(start_dt+timedelta(minutes=duration)).isoformat(timespec="seconds").replace("+00:00","Z")
    slot_id=f"{user_id}#{consultation_id}#{start_utc}"
    now=int(datetime.now(timezone.utc).timestamp())
    item={"slotId":slot_id,"doctorId":str(user_id),"consultationTypeId":consultation_id,
          "startUtc":start_utc,"endUtc":end_utc,"durationMinutes":duration,
          "capacity":capacity,"bookedCount":0,"status":"OPEN","source":"MANUAL",
          "expiresAt":now+90*86400,"createdAt":now,"updatedAt":now}
    try:
        _table("SLOTS_TABLE").put_item(Item=item,ConditionExpression=Attr("slotId").not_exists())
    except ClientError as exc:
        if exc.response.get("Error",{}).get("Code")=="ConditionalCheckFailedException":
            return _response(409,{"message":"A slot already exists for this doctor, consultation type and start time"})
        logger.exception(json.dumps({"event":"slot_create_failed"}))
        return _response(500,{"message":"Unable to create slot"})
    except BotoCoreError:
        logger.exception(json.dumps({"event":"slot_create_failed"}))
        return _response(500,{"message":"Unable to create slot"})
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



def _ssm():
    global _SSM
    if _SSM is None:
        _SSM = boto3.client("ssm")
    return _SSM


def _parameter(name, decrypt=False):
    return _ssm().get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def _db_connection():
    env = os.environ.get("ENVIRONMENT", "dev")
    return pymysql.connect(
        host=_parameter(os.environ.get("DB_HOST_PARAMETER", f"/medislot/{env}/db/host")),
        port=int(_parameter(os.environ.get("DB_PORT_PARAMETER", f"/medislot/{env}/db/port"))),
        user=os.environ["DB_USER"],
        password=_parameter(os.environ.get("DB_PASSWORD_PARAMETER", f"/medislot/{env}/db/app-password"), True),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5, read_timeout=10, write_timeout=10,
        autocommit=False, cursorclass=pymysql.cursors.DictCursor,
    )


def _require_doctor(role):
    return role == "doctor"


def _dispatch_materializer(doctor_id=None, date_from=None, date_to=None):
    function_name = os.environ.get("SLOT_MATERIALIZER_FUNCTION")
    if not function_name:
        logger.warning("SLOT_MATERIALIZER_FUNCTION is not configured; daily schedule will reconcile slots")
        return
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"doctorId": str(doctor_id) if doctor_id else None,
                            "dateFrom": date_from, "dateTo": date_to}).encode(),
    )



def _reference_data(table_name):
    conn=_db_connection()
    try:
        with conn.cursor() as cur:
            if table_name=="specialisations":
                cur.execute("SELECT specialisation_id,name FROM specialisations ORDER BY name")
            else:
                cur.execute("SELECT hospital_id,name,city,address FROM hospitals WHERE status='active' ORDER BY city,name")
            return _response(200,{"items":cur.fetchall()})
    finally: conn.close()


def _my_account(user_id):
    conn=_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id,name,email,role,status,created_at,updated_at FROM users WHERE user_id=%s",(user_id,))
            user=cur.fetchone()
            if not user: return _response(404,{"message":"User not found"})
            if user["role"]=="patient":
                cur.execute("SELECT * FROM patients WHERE user_id=%s",(user_id,))
                user["profile"]=cur.fetchone()
            elif user["role"]=="doctor":
                cur.execute("SELECT * FROM doctors WHERE user_id=%s",(user_id,))
                user["profile"]=cur.fetchone()
            return _response(200,{"user":user})
    finally: conn.close()


def _doctor_profile(user_id, method, event):
    conn = _db_connection()
    try:
        with conn.cursor() as cur:
            if method == "GET":
                cur.execute("""SELECT u.user_id,u.name,u.email,u.status,d.license_number,d.specialisation_id,
                    s.name AS specialisation,d.hospital_id,h.name AS hospital,d.years_experience,
                    d.qualifications,d.languages,d.timezone,d.photo_key
                    FROM users u JOIN doctors d ON d.user_id=u.user_id
                    JOIN specialisations s ON s.specialisation_id=d.specialisation_id
                    LEFT JOIN hospitals h ON h.hospital_id=d.hospital_id
                    WHERE u.user_id=%s AND u.role='doctor'""", (user_id,))
                row=cur.fetchone()
                return _response(200,{"profile":row}) if row else _response(404,{"message":"Doctor profile not found"})
            data=_body(event)
            allowed={"name","licenseNumber","specialisationId","hospitalId","yearsExperience","qualifications","languages","timezone"}
            if not data or set(data)-allowed: return _response(400,{"message":"Only supported doctor profile fields may be updated","allowedFields":sorted(allowed)})
            doctor_map={"licenseNumber":"license_number","specialisationId":"specialisation_id","hospitalId":"hospital_id","yearsExperience":"years_experience","qualifications":"qualifications","languages":"languages","timezone":"timezone"}
            if "name" in data:
                if not isinstance(data["name"],str) or not data["name"].strip() or len(data["name"])>120: return _response(400,{"message":"Invalid name"})
            if not any(key in data for key in ("name", *doctor_map)):
                return _response(400,{"message":"No profile fields supplied"})
            if "name" in data:
                cur.execute("UPDATE users SET name=%s WHERE user_id=%s AND role='doctor'", (data["name"].strip(), user_id))
            # Each SQL statement is static; only values supplied by the caller are bound.
            doctor_update_sql = {
                "licenseNumber": "UPDATE doctors SET license_number=%s WHERE user_id=%s",
                "specialisationId": "UPDATE doctors SET specialisation_id=%s WHERE user_id=%s",
                "hospitalId": "UPDATE doctors SET hospital_id=%s WHERE user_id=%s",
                "yearsExperience": "UPDATE doctors SET years_experience=%s WHERE user_id=%s",
                "qualifications": "UPDATE doctors SET qualifications=%s WHERE user_id=%s",
                "languages": "UPDATE doctors SET languages=%s WHERE user_id=%s",
                "timezone": "UPDATE doctors SET timezone=%s WHERE user_id=%s",
            }
            for key, statement in doctor_update_sql.items():
                if key in data:
                    value = data[key] if data[key] != "" else None
                    cur.execute(statement, (value, user_id))
        conn.commit()
        _dispatch_materializer(user_id)
        return _response(200,{"message":"Doctor profile updated"})
    except pymysql.err.IntegrityError:
        conn.rollback(); return _response(409,{"message":"A unique or foreign-key constraint was violated"})
    finally:
        conn.close()


def _presign_photo(user_id, event):
    data=_body(event); content_type=data.get("contentType")
    if content_type not in {"image/jpeg","image/png","image/webp"}:
        return _response(400,{"message":"contentType must be image/jpeg, image/png or image/webp"})
    s3=boto3.client("s3")
    bucket=os.environ.get("DOCTOR_PHOTOS_BUCKET")
    if not bucket: return _response(500,{"message":"Photo storage is not configured"})
    ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}[content_type]
    key=f"doctors/{user_id}/profile-{uuid.uuid4()}.{ext}"
    url=s3.generate_presigned_url("put_object",Params={"Bucket":bucket,"Key":key,"ContentType":content_type},ExpiresIn=300)
    conn=_db_connection()
    try:
        with conn.cursor() as cur: cur.execute("UPDATE doctors SET photo_key=%s WHERE user_id=%s",(key,user_id))
        conn.commit()
    finally: conn.close()
    return _response(200,{"uploadUrl":url,"photoKey":key,"expiresIn":300})


def _doctor_collection(table_name, user_id, method, event, resource_id=None):
    data=_body(event) if method in {"POST","PUT","PATCH"} else {}
    conn=_db_connection()
    try:
        with conn.cursor() as cur:
            if table_name=="consultation_types":
                if method=="GET":
                    cur.execute("SELECT * FROM consultation_types WHERE doctor_user_id=%s ORDER BY consultation_type_id",(user_id,))
                    return _response(200,{"items":cur.fetchall()})
                if method=="POST":
                    required=("name","durationMin","feeAmount","capacity")
                    if any(k not in data for k in required): return _response(400,{"message":"name, durationMin, feeAmount and capacity are required"})
                    if data["name"] not in {"video","in_clinic","phone"}: return _response(400,{"message":"Invalid consultation name"})
                    cur.execute("""INSERT INTO consultation_types(doctor_user_id,name,duration_min,fee_amount,capacity)
                        VALUES(%s,%s,%s,%s,%s)""",(user_id,data["name"],int(data["durationMin"]),data["feeAmount"],int(data["capacity"])))
                    new_id=cur.lastrowid; conn.commit(); _dispatch_materializer(user_id)
                    return _response(201,{"consultationTypeId":new_id,"message":"Consultation type created"})
                if method in {"PUT","PATCH"} and resource_id:
                    fields={"name":"name","durationMin":"duration_min","feeAmount":"fee_amount","capacity":"capacity","isActive":"is_active"}
                    if set(data) - set(fields):
                        return _response(400,{"message":"Unsupported consultation type fields","allowedFields":sorted(fields)})
                    if not data: return _response(400,{"message":"No supported fields supplied"})
                    update_statements = {
                        "name": "UPDATE consultation_types SET name=%s WHERE consultation_type_id=%s AND doctor_user_id=%s",
                        "durationMin": "UPDATE consultation_types SET duration_min=%s WHERE consultation_type_id=%s AND doctor_user_id=%s",
                        "feeAmount": "UPDATE consultation_types SET fee_amount=%s WHERE consultation_type_id=%s AND doctor_user_id=%s",
                        "capacity": "UPDATE consultation_types SET capacity=%s WHERE consultation_type_id=%s AND doctor_user_id=%s",
                        "isActive": "UPDATE consultation_types SET is_active=%s WHERE consultation_type_id=%s AND doctor_user_id=%s",
                    }
                    cur.execute(
                        "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id=%s AND doctor_user_id=%s",
                        (resource_id, user_id),
                    )
                    if cur.fetchone() is None:
                        return _response(404,{"message":"Consultation type not found"})
                    for key, statement in update_statements.items():
                        if key in data:
                            cur.execute(statement, (data[key], resource_id, user_id))
                    conn.commit(); _dispatch_materializer(user_id); return _response(200,{"message":"Consultation type updated"})
                if method=="DELETE" and resource_id:
                    cur.execute("UPDATE consultation_types SET is_active=0 WHERE consultation_type_id=%s AND doctor_user_id=%s",(resource_id,user_id))
                    conn.commit()
                    return _response(200,{"message":"Consultation type deactivated"}) if cur.rowcount else _response(404,{"message":"Consultation type not found"})
            elif table_name=="availability_patterns":
                if method=="GET":
                    cur.execute("SELECT * FROM availability_patterns WHERE doctor_user_id=%s ORDER BY day_of_week,start_time",(user_id,))
                    return _response(200,{"items":cur.fetchall()})
                if method=="POST":
                    day=int(data["dayOfWeek"]); start_time=data["startTime"]; end_time=data["endTime"]
                    if not 1 <= day <= 7 or end_time <= start_time: return _response(400,{"message":"Invalid weekday or availability time window"})
                    cur.execute("""SELECT pattern_id FROM availability_patterns
                        WHERE doctor_user_id=%s AND consultation_type_id=%s AND day_of_week=%s
                        AND NOT (end_time<=%s OR start_time>=%s) LIMIT 1""",
                        (user_id,int(data["consultationTypeId"]),day,start_time,end_time))
                    if cur.fetchone(): return _response(409,{"message":"This availability window overlaps an existing pattern"})
                    cur.execute("""INSERT INTO availability_patterns(doctor_user_id,consultation_type_id,day_of_week,start_time,end_time)
                        SELECT %s,ct.consultation_type_id,%s,%s,%s FROM consultation_types ct
                        WHERE ct.consultation_type_id=%s AND ct.doctor_user_id=%s AND ct.is_active=1""",
                        (user_id,int(data["dayOfWeek"]),data["startTime"],data["endTime"],int(data["consultationTypeId"]),user_id))
                    if cur.rowcount==0: return _response(400,{"message":"Consultation type does not belong to this doctor"})
                    new_id=cur.lastrowid; conn.commit(); _dispatch_materializer(user_id)
                    return _response(201,{"patternId":new_id,"message":"Availability pattern created"})
                if method in {"PUT","PATCH"} and resource_id:
                    day=int(data["dayOfWeek"]); start_time=data["startTime"]; end_time=data["endTime"]
                    if not 1<=day<=7 or end_time<=start_time: return _response(400,{"message":"Invalid weekday or availability time window"})
                    cur.execute("""SELECT pattern_id FROM availability_patterns WHERE doctor_user_id=%s
                        AND consultation_type_id=%s AND day_of_week=%s AND pattern_id<>%s
                        AND NOT (end_time<=%s OR start_time>=%s) LIMIT 1""",
                        (user_id,int(data["consultationTypeId"]),day,resource_id,start_time,end_time))
                    if cur.fetchone(): return _response(409,{"message":"This availability window overlaps another pattern"})
                    cur.execute("""UPDATE availability_patterns SET consultation_type_id=%s,day_of_week=%s,start_time=%s,end_time=%s
                        WHERE pattern_id=%s AND doctor_user_id=%s""",
                        (int(data["consultationTypeId"]),day,start_time,end_time,resource_id,user_id))
                    if not cur.rowcount: return _response(404,{"message":"Pattern not found"})
                    conn.commit(); _dispatch_materializer(user_id); return _response(200,{"message":"Availability pattern updated"})
                if method=="DELETE" and resource_id:
                    cur.execute("DELETE FROM availability_patterns WHERE pattern_id=%s AND doctor_user_id=%s",(resource_id,user_id))
                    conn.commit(); _dispatch_materializer(user_id)
                    return _response(200,{"message":"Availability pattern deleted"}) if cur.rowcount else _response(404,{"message":"Pattern not found"})
            elif table_name=="blackout_dates":
                if method=="GET":
                    cur.execute("SELECT * FROM blackout_dates WHERE doctor_user_id=%s ORDER BY blackout_date",(user_id,))
                    return _response(200,{"items":cur.fetchall()})
                if method=="POST":
                    cur.execute("INSERT INTO blackout_dates(doctor_user_id,blackout_date,reason) VALUES(%s,%s,%s)",
                        (user_id,data["date"],data.get("reason")))
                    new_id=cur.lastrowid; conn.commit(); _dispatch_materializer(user_id)
                    return _response(201,{"blackoutId":new_id,"message":"Blackout date created"})
                if method in {"PUT","PATCH"} and resource_id:
                    cur.execute("UPDATE blackout_dates SET blackout_date=%s,reason=%s WHERE blackout_id=%s AND doctor_user_id=%s",
                        (data["date"],data.get("reason"),resource_id,user_id))
                    if not cur.rowcount: return _response(404,{"message":"Blackout date not found"})
                    conn.commit(); _dispatch_materializer(user_id); return _response(200,{"message":"Blackout date updated"})
                if method=="DELETE" and resource_id:
                    cur.execute("DELETE FROM blackout_dates WHERE blackout_id=%s AND doctor_user_id=%s",(resource_id,user_id))
                    conn.commit(); _dispatch_materializer(user_id)
                    return _response(200,{"message":"Blackout date deleted"}) if cur.rowcount else _response(404,{"message":"Blackout date not found"})
            elif table_name=="availability_exceptions":
                if method=="GET":
                    cur.execute("SELECT * FROM availability_exceptions WHERE doctor_user_id=%s ORDER BY exception_date",(user_id,))
                    return _response(200,{"items":cur.fetchall()})
                if method=="POST":
                    cur.execute("""INSERT INTO availability_exceptions(doctor_user_id,consultation_type_id,exception_date,exception_type,start_time,end_time)
                        SELECT %s,ct.consultation_type_id,%s,%s,%s,%s FROM consultation_types ct
                        WHERE ct.consultation_type_id=%s AND ct.doctor_user_id=%s""",
                        (user_id,data["date"],data["type"],data.get("startTime"),data.get("endTime"),int(data["consultationTypeId"]),user_id))
                    if cur.rowcount==0: return _response(400,{"message":"Consultation type does not belong to this doctor"})
                    new_id=cur.lastrowid; conn.commit(); _dispatch_materializer(user_id)
                    return _response(201,{"exceptionId":new_id,"message":"Availability exception created"})
                if method in {"PUT","PATCH"} and resource_id:
                    cur.execute("""UPDATE availability_exceptions SET consultation_type_id=%s,exception_date=%s,
                        exception_type=%s,start_time=%s,end_time=%s WHERE exception_id=%s AND doctor_user_id=%s""",
                        (int(data["consultationTypeId"]),data["date"],data["type"],data.get("startTime"),data.get("endTime"),resource_id,user_id))
                    if not cur.rowcount: return _response(404,{"message":"Exception not found"})
                    conn.commit(); _dispatch_materializer(user_id); return _response(200,{"message":"Availability exception updated"})
                if method=="DELETE" and resource_id:
                    cur.execute("DELETE FROM availability_exceptions WHERE exception_id=%s AND doctor_user_id=%s",(resource_id,user_id))
                    conn.commit(); _dispatch_materializer(user_id)
                    return _response(200,{"message":"Availability exception deleted"}) if cur.rowcount else _response(404,{"message":"Exception not found"})
        return _response(405,{"message":"Method not supported for this resource"})
    except (KeyError,TypeError,ValueError) as exc:
        conn.rollback(); return _response(400,{"message":f"Invalid request: {exc}"})
    except pymysql.err.IntegrityError:
        conn.rollback(); return _response(409,{"message":"Duplicate or invalid scheduling record"})
    finally:
        conn.close()


def _doctor_day_schedule(user_id, event):
    params=_query(event); day=params.get("date")
    if not day: return _response(400,{"message":"date query parameter is required (YYYY-MM-DD)"})
    try:
        from datetime import date, timedelta, time as dt_time
        local_date=date.fromisoformat(day)
        conn=_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT timezone FROM doctors WHERE user_id=%s",(user_id,))
                doctor=cur.fetchone()
        finally:
            conn.close()
        if not doctor: return _response(404,{"message":"Doctor profile not found"})
        zone=ZoneInfo(doctor["timezone"])
        local_start=datetime.combine(local_date,dt_time.min).replace(tzinfo=zone)
        local_end=datetime.combine(local_date+timedelta(days=1),dt_time.min).replace(tzinfo=zone)
        start=_iso_utc(local_start.isoformat())
        end=_iso_utc(local_end.isoformat())
        result=_table("SLOTS_TABLE").query(IndexName="gsi-doctor-time",
            KeyConditionExpression=Key("doctorId").eq(str(user_id)) & Key("startUtc").between(start,end))
        return _response(200,{"date":day,"items":result.get("Items",[])})
    except ValueError: return _response(400,{"message":"date must be YYYY-MM-DD"})


def _materializer_trigger(user_id=None):
    try: _dispatch_materializer(user_id)
    except Exception:
        logger.exception("Could not enqueue materializer invocation")
        raise



def _book_appointment(event,user_id,role):
    if role!="patient": return _response(403,{"message":"Only patients can book appointments"})
    data=_body(event); slot_id=data.get("slotId")
    if not isinstance(slot_id,str) or not slot_id.strip(): return _response(400,{"message":"slotId is required"})
    slots=_table("SLOTS_TABLE")
    now=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
    try:
        updated=slots.update_item(Key={"slotId":slot_id},
          UpdateExpression="SET bookedCount=bookedCount+:one, updatedAt=:now",
          ConditionExpression="attribute_exists(slotId) AND #st=:open AND bookedCount < capacity AND startUtc > :now",
          ExpressionAttributeNames={"#st":"status"},
          ExpressionAttributeValues={":one":1,":now":now,":open":"OPEN"},
          ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if exc.response.get("Error",{}).get("Code")=="ConditionalCheckFailedException":
            return _response(409,{"message":"Slot is unavailable, expired or fully booked"})
        logger.exception("Slot capacity update failed")
        return _response(500,{"message":"Unable to reserve slot"})
    except BotoCoreError:
        logger.exception("Slot capacity update failed")
        return _response(500,{"message":"Unable to reserve slot"})
    conn=None
    try:
        conn=_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT fee_amount FROM consultation_types WHERE consultation_type_id=%s AND doctor_user_id=%s AND is_active=1",
                        (int(updated["consultationTypeId"]),int(updated["doctorId"])))
            consultation=cur.fetchone()
            if not consultation: raise ValueError("Consultation type is no longer active")
            start=datetime.fromisoformat(updated["startUtc"].replace("Z","+00:00")).replace(tzinfo=None)
            end=datetime.fromisoformat(updated["endUtc"].replace("Z","+00:00")).replace(tzinfo=None)
            cur.execute("""INSERT INTO appointments
                (patient_user_id,doctor_user_id,consultation_type_id,slot_id,slot_start_utc,slot_end_utc,status,active_flag,fee_amount)
                VALUES(%s,%s,%s,%s,%s,%s,'booked',1,%s)""",
                (int(user_id),int(updated["doctorId"]),int(updated["consultationTypeId"]),slot_id,start,end,consultation["fee_amount"]))
            appointment_id=cur.lastrowid
        conn.commit()
        return _response(201,{"message":"Appointment booked successfully","appointmentId":appointment_id,"slotId":slot_id})
    except Exception:
        if conn:
            try: conn.rollback()
            except Exception: pass
        try:
            slots.update_item(Key={"slotId":slot_id},
              UpdateExpression="SET bookedCount=bookedCount-:one, #st=:open, updatedAt=:now",
              ConditionExpression="bookedCount >= :one",
              ExpressionAttributeNames={"#st":"status"},
              ExpressionAttributeValues={":one":1,":now":now,":open":"OPEN"})
        except Exception: logger.exception("Booking compensation failed; reconcile slot capacity")
        logger.exception("Appointment persistence failed")
        return _response(409,{"message":"Appointment could not be confirmed; slot reservation was released when possible"})
    finally:
        if conn:
            conn.close()


def lambda_handler(event, context):
    request_id=getattr(context,"aws_request_id",None)
    method=str(event.get("httpMethod") or event.get("requestContext",{}).get("http",{}).get("method") or "GET").upper()
    path=_path(event); user_id,role=_principal(event)
    logger.info(json.dumps({"event":"api_request","method":method,"path":path,"requestId":request_id}))
    if path!="/health" and not user_id: return _response(401,{"message":"Unauthorized"})
    try:
        if method=="GET" and path=="/health": return _response(200,{"service":"medislot-api","status":"ok"})
        if method=="GET" and path=="/reference/specialisations": return _reference_data("specialisations")
        if method=="GET" and path=="/reference/hospitals": return _reference_data("hospitals")
        if method=="GET" and path=="/users/me": return _my_account(user_id)
        if path=="/doctors/me" and role=="doctor" and method in {"GET","PUT","PATCH"}:
            return _doctor_profile(user_id,method,event)
        if path=="/doctors/me/photo-upload" and role=="doctor" and method=="POST":
            return _presign_photo(user_id,event)
        for route,table_name in [
            ("/doctors/me/consultation-types","consultation_types"),
            ("/doctors/me/availability-patterns","availability_patterns"),
            ("/doctors/me/blackout-dates","blackout_dates"),
            ("/doctors/me/availability-exceptions","availability_exceptions")]:
            if path==route and role=="doctor":
                return _doctor_collection(table_name,user_id,method,event)
            match=re.fullmatch(re.escape(route)+r"/(\d+)",path)
            if match and role=="doctor":
                return _doctor_collection(table_name,user_id,method,event,int(match.group(1)))
        if path=="/doctors/me/schedule" and method=="GET" and role=="doctor":
            return _doctor_day_schedule(user_id,event)
        if method=="GET" and path=="/slots": return _list_slots(event)
        if method=="POST" and path=="/slots": return _create_slot(event,user_id,role)
        if method=="POST" and path=="/appointments": return _book_appointment(event,user_id,role)
        match=re.fullmatch(r"/slots/([^/]+)",path)
        if method=="GET" and match: return _get_slot(match.group(1))
        if method=="POST" and path=="/waitlist": return _join_waitlist(event,user_id,role)
        if method=="GET" and path=="/waitlist":
            if role!="patient": return _response(403,{"message":"Only patients can view their waitlist"})
            return _my_waitlist(user_id)
        match=re.fullmatch(r"/waitlist/([^/]+)",path)
        if method=="DELETE" and match:
            if role!="patient": return _response(403,{"message":"Only patients can leave their waitlist"})
            return _leave_waitlist(match.group(1),user_id)
        return _response(404,{"message":"Route not found","path":path,"method":method})
    except (ValueError,json.JSONDecodeError,UnicodeDecodeError) as exc:
        return _response(400,{"message":str(exc)})
    except (BotoCoreError,ClientError,pymysql.MySQLError,RuntimeError):
        logger.exception(json.dumps({"event":"api_dependency_error","requestId":request_id}))
        return _response(500,{"message":"Request could not be completed","requestId":request_id})
