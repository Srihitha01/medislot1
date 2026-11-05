"""MediSlot slot materializer.

Generates idempotent slots for a requested local-date range (30 days by
default). It validates each doctor's effective schedule across ALL active
consultation types before writing. Reconciliation is restricted to the same
requested local-date range and only cancels unbooked OPEN materialized slots.
"""
import json
import logging
import os
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
import pymysql
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

try:
    from .availability_rules import effective_windows, validate_doctor_day
except ImportError:  # Lambda handler imports this file as top-level module
    from availability_rules import effective_windows, validate_doctor_day

log = logging.getLogger()
log.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")


def param(name, decrypt=False):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def connection():
    env = os.environ["ENVIRONMENT"]
    return pymysql.connect(
        host=param(os.environ.get("DB_HOST_PARAMETER", f"/medislot/{env}/db/host")),
        port=int(param(os.environ.get("DB_PORT_PARAMETER", f"/medislot/{env}/db/port"))),
        user=os.environ["DB_USER"],
        password=param(os.environ.get("DB_PASSWORD_PARAMETER", f"/medislot/{env}/db/app-password"), True),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=8,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def mysql_time(value):
    if isinstance(value, dt_time):
        return value
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds()) % 86400
        return dt_time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return dt_time.fromisoformat(str(value))


def localize_candidates(naive, zone):
    """Return all valid instants for a local wall-clock time.

    Non-existent DST wall times return no candidates. Ambiguous fall-back
    wall times return both folds, so each real instant can be materialized.
    """
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and all(
            item.astimezone(timezone.utc) != aware.astimezone(timezone.utc)
            for item in candidates
        ):
            candidates.append(aware)
    return candidates


def fetch_data(doctor_id=None):
    """Load all consultation schedules so cross-type overlaps can be checked."""
    conn = connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.user_id, d.timezone,
                       ct.consultation_type_id, ct.duration_min,
                       ct.capacity, ct.is_active
                FROM doctors d
                JOIN consultation_types ct ON ct.doctor_user_id = d.user_id
                WHERE (%s IS NULL OR d.user_id = %s)
                ORDER BY d.user_id, ct.consultation_type_id
                """,
                (doctor_id, doctor_id),
            )
            types = cur.fetchall()
            result = []
            doctor_blackouts = {}

            for item in types:
                did = item["user_id"]
                cid = item["consultation_type_id"]
                cur.execute(
                    """
                    SELECT day_of_week, start_time, end_time
                    FROM availability_patterns
                    WHERE doctor_user_id=%s AND consultation_type_id=%s
                    ORDER BY day_of_week, start_time
                    """,
                    (did, cid),
                )
                patterns = cur.fetchall()
                cur.execute(
                    """
                    SELECT exception_date, exception_type, start_time, end_time
                    FROM availability_exceptions
                    WHERE doctor_user_id=%s AND consultation_type_id=%s
                    ORDER BY exception_date, start_time
                    """,
                    (did, cid),
                )
                exceptions = cur.fetchall()

                if did not in doctor_blackouts:
                    cur.execute(
                        "SELECT blackout_date FROM blackout_dates WHERE doctor_user_id=%s",
                        (did,),
                    )
                    doctor_blackouts[did] = [row["blackout_date"] for row in cur.fetchall()]

                result.append({
                    **item,
                    "patterns": patterns,
                    "exceptions": exceptions,
                    "blackouts": doctor_blackouts[did],
                })
            return result
    finally:
        conn.close()


def _schedule_slots_for_day(schedule, target_day):
    """Pure slot generation for one consultation type and local date."""
    if not schedule.get("is_active", True):
        return []

    # Blackout days still generate slots, but they are marked BLACKOUT so they
    # show as unavailable and can never be booked (booking requires OPEN).
    is_blackout = any(
        (item.date() if isinstance(item, datetime) else item) == target_day
        if not isinstance(item, str)
        else date.fromisoformat(item[:10]) == target_day
        for item in (schedule.get("blackouts") or [])
    )
    slot_status = "BLACKOUT" if is_blackout else "OPEN"

    windows = effective_windows(
        schedule.get("patterns", []),
        schedule.get("exceptions", []),
        [],
        target_day,
    )
    if not windows:
        return []

    timezone_name = schedule.get("timezone") or "Asia/Kolkata"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown doctor timezone: {timezone_name}") from exc

    duration = int(schedule["duration_min"])
    capacity = int(schedule["capacity"])
    if duration <= 0 or capacity <= 0:
        raise ValueError("Consultation duration and capacity must be positive")

    slots = []
    for start_value, end_value in windows:
        start_time = mysql_time(start_value)
        end_time = mysql_time(end_value)
        local_start = datetime.combine(target_day, start_time)
        local_end = datetime.combine(target_day, end_time)

        end_candidates = localize_candidates(local_end, zone)
        if not end_candidates:
            # An end boundary inside a DST spring-forward gap is invalid.
            continue
        end_limit = max(candidate.astimezone(timezone.utc) for candidate in end_candidates)

        cursor = local_start
        while cursor + timedelta(minutes=duration) <= local_end:
            for aware_start in localize_candidates(cursor, zone):
                end_instant = aware_start.astimezone(timezone.utc) + timedelta(minutes=duration)
                if end_instant <= end_limit:
                    start_utc = utc_iso(aware_start)
                    end_utc = utc_iso(end_instant)
                    doctor_id = str(schedule["user_id"])
                    consultation_id = int(schedule["consultation_type_id"])
                    slot_id = f"{doctor_id}#{consultation_id}#{start_utc}"
                    expiry = int(datetime.combine(
                        target_day + timedelta(days=90), dt_time.min,
                        tzinfo=timezone.utc
                    ).timestamp())
                    slots.append({
                        "slotId": slot_id,
                        "doctorId": doctor_id,
                        "consultationTypeId": str(consultation_id),
                        "startUtc": start_utc,
                        "endUtc": end_utc,
                        "localDate": target_day.isoformat(),
                        "timezone": timezone_name,
                        "utcOffset": aware_start.strftime("%z"),
                        "localStartTime": cursor.strftime("%H:%M:%S"),
                        "durationMinutes": duration,
                        "capacity": capacity,
                        "bookedCount": 0,
                        "status": slot_status,
                        "source": "MATERIALIZED",
                        "expiresAt": expiry,
                        "createdAt": int(time.time()),
                        "updatedAt": int(time.time()),
                    })
            cursor += timedelta(minutes=duration)
    return slots


def _query_slots_for_doctor(table, doctor_id, date_from, date_to):
    """Query only the UTC envelope that can contain the requested local dates."""
    lower = utc_iso(datetime.combine(date_from - timedelta(days=2), dt_time.min, tzinfo=timezone.utc))
    upper = utc_iso(datetime.combine(date_to + timedelta(days=2), dt_time.max, tzinfo=timezone.utc))
    result = table.query(
        IndexName="gsi-doctor-time",
        KeyConditionExpression=Key("doctorId").eq(doctor_id) & Key("startUtc").between(lower, upper),
    )
    items = list(result.get("Items", []))
    while result.get("LastEvaluatedKey"):
        result = table.query(
            IndexName="gsi-doctor-time",
            KeyConditionExpression=Key("doctorId").eq(doctor_id) & Key("startUtc").between(lower, upper),
            ExclusiveStartKey=result["LastEvaluatedKey"],
        )
        items.extend(result.get("Items", []))
    return items


def _put_slot(table, item):
    """Create once; reopen only a previously cancelled, still-unbooked slot."""
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(slotId)")
        return "created"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            raise

    # An existing unbooked generated slot may change between OPEN, BLACKOUT
    # and CANCELLED. Never overwrite booked/full/manual records.
    try:
        table.update_item(
            Key={"slotId": item["slotId"]},
            UpdateExpression=(
                "SET #st=:target, #cap=:capacity, #upd=:now, "
                "#exp=:expiry, #ld=:localDate, #tz=:timezone, "
                "#off=:offset, #lst=:localTime"
            ),
            ConditionExpression=(
                "#bc=:zero AND #src=:source AND #st <> :target "
                "AND #st IN (:open, :cancelled, :blackout)"
            ),
            ExpressionAttributeNames={
                "#st": "status", "#src": "source", "#cap": "capacity",
                "#upd": "updatedAt", "#exp": "expiresAt", "#ld": "localDate",
                "#tz": "timezone", "#off": "utcOffset",
                "#lst": "localStartTime", "#bc": "bookedCount",
            },
            ExpressionAttributeValues={
                ":target": item["status"], ":capacity": item["capacity"],
                ":now": int(time.time()), ":expiry": item["expiresAt"],
                ":localDate": item["localDate"], ":timezone": item["timezone"],
                ":offset": item["utcOffset"], ":localTime": item["localStartTime"],
                ":zero": 0, ":source": "MATERIALIZED",
                ":open": "OPEN", ":cancelled": "CANCELLED", ":blackout": "BLACKOUT",
            },
        )
        return "reopened"
    except ClientError as update_exc:
        if update_exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "existing"
        raise


def _reconcile_window(table, doctor_id, date_from, date_to, expected_ids):
    """Cancel stale open/unbooked generated slots inside this date range only."""
    cancelled = 0
    preserved_booked = 0
    now_iso = utc_iso(datetime.now(timezone.utc))
    now_epoch = int(time.time())

    for slot in _query_slots_for_doctor(table, doctor_id, date_from, date_to):
        local_date_value = slot.get("localDate")
        if not local_date_value:
            continue
        try:
            local_day = date.fromisoformat(str(local_date_value)[:10])
        except ValueError:
            continue
        if not date_from <= local_day <= date_to:
            continue
        if slot.get("source") != "MATERIALIZED" or slot.get("slotId") in expected_ids:
            continue
        if slot.get("startUtc", "") < now_iso:
            continue
        if int(slot.get("bookedCount", 0)) > 0:
            preserved_booked += 1
            continue
        if slot.get("status") not in ("OPEN", "BLACKOUT"):
            continue

        try:
            table.update_item(
                Key={"slotId": slot["slotId"]},
                UpdateExpression="SET #st=:cancelled, #upd=:now",
                ConditionExpression=(
                    "#bc=:zero AND #st IN (:open, :blackout) AND #src=:source"
                ),
                ExpressionAttributeNames={
                    "#st": "status", "#src": "source",
                    "#upd": "updatedAt", "#bc": "bookedCount",
                },
                ExpressionAttributeValues={
                    ":cancelled": "CANCELLED", ":now": now_epoch,
                    ":zero": 0, ":open": "OPEN", ":blackout": "BLACKOUT",
                    ":source": "MATERIALIZED",
                },
            )
            cancelled += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return cancelled, preserved_booked


def lambda_handler(event, context):
    event = event or {}
    doctor_id = event.get("doctorId")

    # A request may explicitly scope a smaller/larger window, capped at 60 days.
    schedules = fetch_data(doctor_id)
    if event.get("dateFrom"):
        date_from = date.fromisoformat(event["dateFrom"])
    else:
        # Use the first doctor's local date where possible; otherwise UTC.
        if schedules:
            zone_name = schedules[0].get("timezone") or "Asia/Kolkata"
            date_from = datetime.now(ZoneInfo(zone_name)).date()
        else:
            date_from = datetime.now(timezone.utc).date()

    date_to = (
        date.fromisoformat(event["dateTo"])
        if event.get("dateTo")
        else date_from + timedelta(days=int(os.environ.get("SLOT_WINDOW_DAYS", "30")) - 1)
    )
    if date_to < date_from:
        raise ValueError("dateTo must be on or after dateFrom")
    if (date_to - date_from).days > 60:
        raise ValueError("Materialisation window cannot exceed 60 days")

    table = ddb.Table(os.environ["SLOTS_TABLE"])
    doctor_ids = sorted({str(row["user_id"]) for row in schedules})

    # Validate the entire requested date range before writing any slots.
    day = date_from
    while day <= date_to:
        validate_doctor_day(schedules, day)
        day += timedelta(days=1)

    expected_by_doctor = {did: set() for did in doctor_ids}
    generated = []
    day = date_from
    while day <= date_to:
        for schedule in schedules:
            if not schedule.get("is_active", True):
                continue
            for slot in _schedule_slots_for_day(schedule, day):
                generated.append(slot)
                expected_by_doctor[slot["doctorId"]].add(slot["slotId"])
        day += timedelta(days=1)

    created = 0
    reopened = 0
    for item in generated:
        result = _put_slot(table, item)
        if result == "created":
            created += 1
        elif result == "reopened":
            reopened += 1

    cancelled = 0
    preserved_booked = 0
    for did in doctor_ids:
        removed, preserved = _reconcile_window(
            table, did, date_from, date_to, expected_by_doctor[did]
        )
        cancelled += removed
        preserved_booked += preserved

    result = {
        "status": "ok",
        "doctorId": str(doctor_id) if doctor_id else None,
        "created": created,
        "reopened": reopened,
        "examined": len(generated),
        "cancelled": cancelled,
        "preservedBooked": preserved_booked,
        "windowStart": date_from.isoformat(),
        "windowEnd": date_to.isoformat(),
    }
    log.info(json.dumps({"event": "slot_materialisation_complete", **result}))
    return result