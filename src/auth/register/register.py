
import json
import logging
import os
import re
import boto3
import bcrypt
import pymysql

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "10"))
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": (
                "Content-Type,Authorization,Idempotency-Key,X-Correlation-Id"
            ),
            "Access-Control-Allow-Methods": (
                "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            )
        },
        "body": json.dumps(body)
    }


def _parameter(name, decrypt=False):
    return ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )["Parameter"]["Value"]


def db():
    env = os.environ["ENVIRONMENT"]

    return pymysql.connect(
        host=_parameter(
            os.environ.get(
                "DB_HOST_PARAMETER",
                f"/medislot/{env}/db/host"
            )
        ),
        port=int(_parameter(
            os.environ.get(
                "DB_PORT_PARAMETER",
                f"/medislot/{env}/db/port"
            )
        )),
        user=os.environ["DB_USER"],
        password=_parameter(
            os.environ.get(
                "DB_PASSWORD_PARAMETER",
                f"/medislot/{env}/db/app-password"
            ),
            True
        ),
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor
    )


def body(event):
    raw = event.get("body") or "{}"
    value = json.loads(raw) if isinstance(raw, str) else raw

    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")

    return value


def lambda_handler(event, context):
    conn = None

    try:
        data = body(event)

        # Both doctors and patients use this same endpoint.
        role = data.get("role")

        if not isinstance(role, str):
            return response(400, {
                "message": "role is required and must be doctor or patient"
            })

        role = role.strip().lower()

        if role not in ("doctor", "patient"):
            return response(400, {
                "message": "Invalid role. Allowed values: doctor, patient"
            })

        name = data.get("name")
        email = data.get("email")
        password = data.get("password")

        if not all(
            isinstance(value, str)
            for value in (name, email, password)
        ):
            return response(400, {
                "message": "name, email and password are required"
            })

        name = name.strip()
        email = email.strip().lower()

        if not name or len(name) > 120:
            return response(400, {
                "message": "name must contain 1 to 120 characters"
            })

        if not EMAIL_RE.fullmatch(email) or len(email) > 254:
            return response(400, {
                "message": "Invalid email address"
            })

        password_bytes = password.encode("utf-8")

        if (
            len(password) < PASSWORD_MIN_LENGTH
            or len(password_bytes) > 72
            or not re.search(r"[A-Za-z]", password)
            or not re.search(r"\d", password)
        ):
            return response(400, {
                "message": (
                    f"Password must be at least {PASSWORD_MIN_LENGTH} "
                    "characters and contain a letter and a number "
                    "(maximum 72 UTF-8 bytes)"
                )
            })

        # Role-specific required fields.
        if role == "patient":
            required = [
                "dateOfBirth",
                "gender",
                "phone",
                "city"
            ]
        else:
            required = [
                "licenseNumber",
                "specialisationId",
                "yearsExperience",
                "qualifications",
                "languages",
                "timezone"
            ]

        missing = [
            key for key in required
            if data.get(key) is None or data.get(key) == ""
        ]

        if missing:
            return response(400, {
                "message": "Missing required registration fields",
                "fields": missing
            })

        if role == "patient":

            if data["gender"] not in {
                "female",
                "male",
                "non_binary",
                "prefer_not_to_say"
            }:
                return response(400, {
                    "message": "Invalid gender"
                })

        else:
            try:
                data["specialisationId"] = int(
                    data["specialisationId"]
                )

                data["yearsExperience"] = int(
                    data["yearsExperience"]
                )

                if not 0 <= data["yearsExperience"] <= 80:
                    raise ValueError()

                if data.get("hospitalId") not in (None, ""):
                    data["hospitalId"] = int(data["hospitalId"])
                else:
                    data["hospitalId"] = None

            except (TypeError, ValueError):
                return response(400, {
                    "message": (
                        "specialisationId, hospitalId and "
                        "yearsExperience must be valid integers"
                    )
                })

            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(data["timezone"])
            except (ZoneInfoNotFoundError, TypeError):
                return response(400, {
                    "message": (
                        "timezone must be a valid IANA timezone, "
                        "for example Asia/Kolkata"
                    )
                })

            if (
                not isinstance(data["licenseNumber"], str)
                or not data["licenseNumber"].strip()
                or len(data["licenseNumber"]) > 50
            ):
                return response(400, {
                    "message": (
                        "licenseNumber must contain 1 to 50 characters"
                    )
                })

            if not all(
                isinstance(data[key], str) and data[key].strip()
                for key in ("qualifications", "languages", "timezone")
            ):
                return response(400, {
                    "message": (
                        "qualifications, languages and timezone "
                        "must be non-empty strings"
                    )
                })

        # The database numbers each new user (1, 2, 3 ...). That number is the
        # identity shared by the users table and the role-specific profile.

        # Create the account and role-specific profile in one transaction.
        conn = db()

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users
                    (name, email, password_hash, role, status)
                VALUES (%s, %s, %s, %s, 'active')
                """,
                (
                    name,
                    email,
                    bcrypt.hashpw(
                        password_bytes,
                        bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
                    ).decode(),
                    role
                )
            )

            # The new user's number, assigned by MySQL.
            user_id = cur.lastrowid

            if role == "patient":
                cur.execute(
                    """
                    INSERT INTO patients
                    (
                        user_id,
                        full_name,
                        date_of_birth,
                        gender,
                        phone,
                        city,
                        emergency_contact_name,
                        emergency_contact_phone,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        name,
                        data["dateOfBirth"],
                        data["gender"],
                        data["phone"],
                        data["city"],
                        data.get("emergencyContactName"),
                        data.get("emergencyContactPhone"),
                        data.get("notes")
                    )
                )

            else:
                cur.execute(
                    """
                    INSERT INTO doctors
                    (
                        user_id,
                        full_name,
                        license_number,
                        specialisation_id,
                        hospital_id,
                        years_experience,
                        qualifications,
                        languages,
                        timezone,
                        photo_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        user_id,
                        name,
                        data["licenseNumber"].strip(),
                        data["specialisationId"],
                        data.get("hospitalId"),
                        data["yearsExperience"],
                        data["qualifications"],
                        data["languages"],
                        data["timezone"]
                    )
                )

        conn.commit()

        logger.info(json.dumps({
            "event": "registration_succeeded",
            "userId": user_id,
            "role": role
        }))

        return response(201, {
            "message": f"{role.capitalize()} registration successful",
            "userId": user_id,
            "role": role
        })

    except pymysql.err.IntegrityError as exc:
        if conn:
            conn.rollback()

        code = exc.args[0] if exc.args else None

        if code == 1062:
            return response(409, {
                "message": (
                    "Email or professional license number already exists"
                )
            })

        if code == 1452:
            return response(400, {
                "message": (
                    "Selected specialisation or hospital does not exist"
                )
            })

        logger.exception("Registration integrity error")

        return response(400, {
            "message": "Registration details violate a database constraint"
        })

    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return response(400, {
            "message": str(exc) or "Invalid JSON request"
        })

    except (BotoCoreError, ClientError, pymysql.MySQLError):
        if conn:
            conn.rollback()

        logger.exception("Registration dependency failure")

        return response(500, {
            "message": "Registration could not be completed"
        })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("Unexpected registration failure")

        return response(500, {
            "message": "Registration could not be completed"
        })

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
