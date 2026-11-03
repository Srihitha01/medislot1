"""LEGACY MODULE: active doctor endpoints are implemented in src/api/app.py.

This module is not referenced by the current CloudFormation templates.
"""

import json
import logging
import os
from datetime import date, datetime, time
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

import boto3
import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
        },
        "body": json.dumps(body, default=json_serializer),
    }


def json_serializer(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


@lru_cache(maxsize=8)
def get_parameter(name):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    environment = os.environ["ENVIRONMENT"]

    host = get_parameter(
        os.environ.get(
            "DB_HOST_PARAMETER",
            f"/medislot/{environment}/db/host"
        )
    )

    port = int(get_parameter(
        os.environ.get(
            "DB_PORT_PARAMETER",
            f"/medislot/{environment}/db/port"
        )
    ))

    password = get_parameter(
        os.environ.get(
            "DB_PASSWORD_PARAMETER",
            f"/medislot/{environment}/db/app-password"
        )
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=os.environ.get("DB_USER", "medislot_app"),
        password=password,
        database=os.environ.get("DB_NAME", "medislot"),
        cursorclass=DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=True,
        charset="utf8mb4",
    )


def get_user_identity(event):
    """
    API Gateway TOKEN authorizer context.

    The authorizer must provide:
      principalId = authenticated users.user_id
      context.role = doctor or patient

    The database uses doctors.user_id = users.user_id.
    """
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    role = authorizer.get("role")
    user_id = (
        authorizer.get("user_id")
        or authorizer.get("userId")
        or authorizer.get("doctor_id")
        or authorizer.get("principalId")
    )

    # API Gateway may expose principalId separately from context.
    if not user_id:
        user_id = request_context.get("authorizer", {}).get(
            "principalId"
        )

    if role != "doctor" or not user_id:
        return None

    return str(user_id)


def get_doctor_profile(cursor, user_id):
    sql = """
        SELECT
            d.user_id,
            d.full_name,
            d.license_number,
            d.specialisation_id,
            s.name AS specialisation,
            d.hospital_id,
            h.name AS hospital_name,
            d.years_experience,
            d.qualifications,
            d.languages,
            d.timezone,
            d.photo_key,
            d.created_at,
            d.updated_at
        FROM doctors d
        LEFT JOIN specialisations s
            ON s.specialisation_id = d.specialisation_id
        LEFT JOIN hospitals h
            ON h.hospital_id = d.hospital_id
        WHERE d.user_id = %s
        LIMIT 1
    """
    cursor.execute(sql, (user_id,))
    return cursor.fetchone()


def get_dashboard(cursor, user_id, timezone_name):
    try:
        doctor_timezone = ZoneInfo("Asia/Kolkata")
    except Exception:
        doctor_timezone = ZoneInfo("Asia/Kolkata")

    now_local = datetime.now(doctor_timezone)
    start_local = datetime.combine(
        now_local.date(),
        time.min,
        tzinfo=doctor_timezone
    )
    end_local = start_local.replace(
        day=1
    ) if False else start_local.replace(
        hour=0, minute=0, second=0
    )

    # Convert the doctor's local calendar day to UTC.
    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(
        tzinfo=None
    )
    end_utc = (
        start_local.replace(day=now_local.day)
        .replace(hour=0, minute=0, second=0)
        + __import__("datetime").timedelta(days=1)
    ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    cursor.execute(
        """
        SELECT
            COUNT(*) AS today_appointments,
            SUM(CASE WHEN status IN ('CONFIRMED', 'booked') THEN 1 ELSE 0 END)
                AS confirmed_today
        FROM appointments
        WHERE doctor_user_id = %s
          AND slot_start_utc >= %s
          AND slot_start_utc < %s
        """,
        (user_id, start_utc, end_utc)
    )
    today = cursor.fetchone() or {}

    cursor.execute(
        """
        SELECT COUNT(DISTINCT patient_user_id) AS total_patients
        FROM appointments
        WHERE doctor_user_id = %s
        """,
        (user_id,)
    )
    patients = cursor.fetchone() or {}

    cursor.execute(
        """
        SELECT COUNT(*) AS upcoming_appointments
        FROM appointments
        WHERE doctor_user_id = %s
          AND status IN ('CONFIRMED', 'booked')
          AND slot_start_utc >= UTC_TIMESTAMP(3)
        """,
        (user_id,)
    )
    upcoming = cursor.fetchone() or {}

    cursor.execute(
        """
        SELECT COUNT(*) AS consultation_types
        FROM consultation_types
        WHERE doctor_user_id = %s
          AND is_active = 1
        """,
        (user_id,)
    )
    consultations = cursor.fetchone() or {}

    return {
        "today_appointments": today.get("today_appointments") or 0,
        "confirmed_today": today.get("confirmed_today") or 0,
        "total_patients": patients.get("total_patients") or 0,
        "upcoming_appointments": upcoming.get(
            "upcoming_appointments"
        ) or 0,
        "consultation_types": consultations.get(
            "consultation_types"
        ) or 0,
        "local_date": now_local.date().isoformat(),
        "timezone": "Asia/Kolkata",
    }


def get_appointments(cursor, user_id, query):
    limit = min(max(int(query.get("limit", 50)), 1), 100)

    sql = """
        SELECT
            a.appointment_id,
            a.patient_user_id AS patient_id,
            p.full_name AS patient_name,
            p.phone AS patient_phone,
            a.consultation_type_id,
            ct.name AS consultation_type,
            ct.duration_min,
            a.slot_start_utc,
            a.slot_end_utc,
            a.status,
            a.fee_amount
        FROM appointments a
        JOIN patients p
            ON p.user_id = a.patient_user_id
        JOIN consultation_types ct
            ON ct.consultation_type_id = a.consultation_type_id
        WHERE a.doctor_user_id = %s
    """
    parameters = [user_id]

    status = query.get("status")
    if status:
        allowed_statuses = {
            "CONFIRMED",
            "COMPLETED",
            "CANCELLED_BY_PATIENT",
            "CANCELLED_BY_DOCTOR",
            "NO_SHOW",
        }
        if status not in allowed_statuses:
            raise ValueError("Invalid appointment status")
        sql += " AND a.status = %s"
        parameters.append(status)

    sql += " ORDER BY a.slot_start_utc DESC LIMIT %s"
    parameters.append(limit)

    cursor.execute(sql, tuple(parameters))
    return cursor.fetchall()


def get_patients(cursor, user_id, query):
    limit = min(max(int(query.get("limit", 50)), 1), 100)

    sql = """
        SELECT
            p.user_id AS patient_id,
            p.full_name,
            p.phone,
            p.city,
            COUNT(a.appointment_id) AS appointment_count,
            MAX(a.slot_start_utc) AS last_appointment
        FROM appointments a
        JOIN patients p
            ON p.user_id = a.patient_user_id
        WHERE a.doctor_user_id = %s
        GROUP BY
            p.user_id,
            p.full_name,
            p.phone,
            p.city
        ORDER BY last_appointment DESC
        LIMIT %s
    """
    cursor.execute(sql, (user_id, limit))
    return cursor.fetchall()


def get_consultation_types(cursor, user_id):
    cursor.execute(
        """
        SELECT
            consultation_type_id,
            name,
            duration_min,
            fee_amount,
            capacity,
            is_active
        FROM consultation_types
        WHERE doctor_user_id = %s
          AND is_active = 1
        ORDER BY duration_min
        """,
        (user_id,)
    )
    return cursor.fetchall()


def get_availability(cursor, user_id):
    cursor.execute(
        """
        SELECT
            pattern_id,
            consultation_type_id,
            day_of_week,
            start_time,
            end_time
        FROM availability_patterns
        WHERE doctor_user_id = %s
        ORDER BY day_of_week, start_time
        """,
        (user_id,)
    )
    return cursor.fetchall()


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")

    try:
        user_id = get_user_identity(event)

        if not user_id:
            return response(403, {
                "message": "Doctor access is required."
            })

        method = event.get("httpMethod", "")
        path = event.get("path", "")
        query = event.get("queryStringParameters") or {}

        if method == "OPTIONS":
            return response(200, {"message": "OK"})

        if method != "GET":
            return response(405, {
                "message": "Method not supported by this version."
            })

        connection = get_db_connection()

        try:
            with connection.cursor() as cursor:
                profile = get_doctor_profile(cursor, user_id)

                if not profile:
                    return response(404, {
                        "message": "Doctor profile not found."
                    })

                if path == "/doctors/me":
                    return response(200, {"doctor": profile})

                if path == "/doctors/me/dashboard":
                    dashboard = get_dashboard(
                        cursor,
                        user_id,
                        "Asia/Kolkata"
                    )
                    return response(200, {"dashboard": dashboard})

                if path == "/doctors/me/appointments":
                    appointments = get_appointments(
                        cursor, user_id, query
                    )
                    return response(200, {
                        "appointments": appointments,
                        "count": len(appointments)
                    })

                if path == "/doctors/me/patients":
                    patients = get_patients(cursor, user_id, query)
                    return response(200, {
                        "patients": patients,
                        "count": len(patients)
                    })

                if path == "/doctors/me/consultation-types":
                    types = get_consultation_types(cursor, user_id)
                    return response(200, {
                        "consultation_types": types
                    })

                if path == "/doctors/me/availability":
                    availability = get_availability(
                        cursor, user_id
                    )
                    return response(200, {
                        "availability": availability
                    })

                return response(404, {
                    "message": "Doctor API route not found."
                })

        finally:
            connection.close()

    except ValueError as exc:
        return response(400, {"message": str(exc)})

    except Exception:
        logger.exception(
            "Doctor Lambda request failed. request_id=%s",
            request_id
        )
        return response(500, {
            "message": "An internal server error occurred.",
            "request_id": request_id
        })
