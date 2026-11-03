
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

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")

DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.getenv("DB_USER")

DB_HOST_PARAMETER = os.getenv(
    "DB_HOST_PARAMETER",
    f"/medislot/{ENVIRONMENT}/db/host"
)

DB_PORT_PARAMETER = os.getenv(
    "DB_PORT_PARAMETER",
    f"/medislot/{ENVIRONMENT}/db/port"
)

DB_PASSWORD_PARAMETER = os.getenv(
    "DB_PASSWORD_PARAMETER",
    f"/medislot/{ENVIRONMENT}/db/app-password"
)

DB_USER_PARAMETER = os.getenv("DB_USER_PARAMETER")

BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))

EMAIL_RE = re.compile(
    r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
)

MAX_UNSIGNED_BIGINT = 18446744073709551615


# ---------------------------------------------------------
# LAZY AWS CLIENT INITIALIZATION
# ---------------------------------------------------------

_ssm = None


def get_ssm_client():
    """
    Initialize SSM only when required.
    This prevents AWS credential errors during module import.
    """

    global _ssm

    if _ssm is None:
        _ssm = boto3.client(
            "ssm",
            region_name=AWS_REGION
        )

    return _ssm


def get_parameter(parameter_name):
    """
    Retrieve a parameter from SSM Parameter Store.
    """

    response = get_ssm_client().get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    return response["Parameter"]["Value"]


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def get_db_connection():
    """
    Retrieve database configuration from SSM and connect
    to the MediSlot MySQL database.
    """

    db_host = get_parameter(DB_HOST_PARAMETER)
    db_port = int(get_parameter(DB_PORT_PARAMETER))
    db_password = get_parameter(DB_PASSWORD_PARAMETER)

    db_user = DB_USER

    if not db_user:
        if not DB_USER_PARAMETER:
            raise ValueError(
                "DB_USER or DB_USER_PARAMETER must be configured"
            )

        db_user = get_parameter(DB_USER_PARAMETER)

    return pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=DB_NAME,
        cursorclass=pymysql.cursors.Cursor,
        autocommit=False,
        connect_timeout=10
    )


# Stable connection seam used by the handler and unit tests.
# Kept separate from module import so no AWS calls occur during import.
def db():
    return get_db_connection()


# ---------------------------------------------------------
# INPUT VALIDATION
# ---------------------------------------------------------

def validate_registration(data):
    """
    Validate registration request fields.
    """

    if not isinstance(data, dict):
        raise ValueError("Invalid request body")

    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    role = data.get("role")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Name is required")

    name = name.strip()

    if len(name) > 150:
        raise ValueError("Name must not exceed 150 characters")

    if not isinstance(email, str):
        raise ValueError("Email is required")

    email = email.strip().lower()

    if not EMAIL_RE.fullmatch(email):
        raise ValueError("Invalid email address")

    if len(email) > 255:
        raise ValueError("Email must not exceed 255 characters")

    if not isinstance(password, str):
        raise ValueError("Password is required")

    if len(password) < 8:
        raise ValueError("Password must contain at least 8 characters")

    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password exceeds bcrypt's 72-byte limit")

    if not isinstance(role, str):
        raise ValueError("Role is required")

    role = role.strip().lower()

    if role not in ("patient", "doctor"):
        raise ValueError("Role must be patient or doctor")

    return {
        "name": name,
        "email": email,
        "password": password,
        "role": role
    }


# ---------------------------------------------------------
# PASSWORD HASHING
# ---------------------------------------------------------

def hash_password(password):
    """
    Hash the password using bcrypt.
    """

    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        salt
    )

    return password_hash.decode("utf-8")


# ---------------------------------------------------------
# REGISTRATION
# ---------------------------------------------------------

def register_user(data):
    """
    Create a user and the corresponding patient or doctor
    profile using the same sequential numeric user ID.

    user_id is generated by MySQL AUTO_INCREMENT.
    """

    validated = validate_registration(data)

    name = validated["name"]
    email = validated["email"]
    password = validated["password"]
    role = validated["role"]

    password_hash = hash_password(password)

    connection = db()

    try:
        with connection.cursor() as cursor:

            # Check whether the email already exists.
            cursor.execute(
                """
                SELECT user_id
                FROM users
                WHERE email = %s
                LIMIT 1
                """,
                (email,)
            )

            existing_user = cursor.fetchone()

            if existing_user:
                connection.rollback()

                return {
                    "statusCode": 409,
                    "body": json.dumps({
                        "message": "Email is already registered"
                    })
                }

            # Insert user without manually supplying user_id.
            cursor.execute(
                """
                INSERT INTO users (
                    name,
                    email,
                    password_hash,
                    role
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    name,
                    email,
                    password_hash,
                    role
                )
            )

            # Obtain the sequential numeric ID from MySQL.
            user_id = int(cursor.lastrowid)

            if user_id <= 0 or user_id > MAX_UNSIGNED_BIGINT:
                raise ValueError("Invalid generated user ID")

            # Create the corresponding profile using the
            # exact same numeric user_id.
            if role == "patient":
                cursor.execute(
                    """
                    INSERT INTO patients (user_id, full_name, date_of_birth,
                                          gender, phone, city,
                                          emergency_contact_name,
                                          emergency_contact_phone, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        data.get("name", "").strip(),
                        data.get("dateOfBirth") or None,
                        data.get("gender") or None,
                        data.get("phone") or None,
                        data.get("city") or None,
                        data.get("emergencyContactName") or None,
                        data.get("emergencyContactPhone") or None,
                        data.get("notes") or None
                    )
                )

            elif role == "doctor":
                required_doctor_fields = {
                    "licenseNumber": data.get("licenseNumber"),
                    "specialisationId": data.get("specialisationId")
                }
                if not required_doctor_fields["licenseNumber"]:
                    raise ValueError("Doctor license number is required")
                if not required_doctor_fields["specialisationId"]:
                    raise ValueError("Doctor specialisation is required")

                cursor.execute(
                    """
                    INSERT INTO doctors (
                        user_id, full_name, license_number,
                        specialisation_id, hospital_id, years_experience,
                        qualifications, languages, timezone
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        data.get("name", "").strip(),
                        str(data["licenseNumber"]).strip(),
                        int(data["specialisationId"]),
                        data.get("hospitalId") or None,
                        data.get("yearsExperience") or None,
                        data.get("qualifications") or None,
                        data.get("languages") or None,
                        data.get("timezone") or "UTC"
                    )
                )

            connection.commit()

            logger.info(
                "Registration successful: user_id=%s, role=%s",
                user_id,
                role
            )

            return {
                "statusCode": 201,
                "body": json.dumps({
                    "message": "Registration successful",
                    "userId": user_id,
                    "role": role
                })
            }

    except pymysql.err.IntegrityError as exc:
        connection.rollback()

        # Duplicate email or another database constraint.
        if exc.args and exc.args[0] == 1062:
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "message": "Email is already registered"
                })
            }

        logger.exception("Database integrity error during registration")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Registration failed"
            })
        }

    except Exception:
        connection.rollback()

        logger.exception("Registration failed")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Registration failed"
            })
        }

    finally:
        connection.close()


# ---------------------------------------------------------
# LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    """
    API Gateway Lambda entry point.
    """

    try:
        body = event.get("body")

        if isinstance(body, str):
            data = json.loads(body)
        elif isinstance(body, dict):
            data = body
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "message": "Invalid request body"
                })
            }

        result = register_user(data)

        return result

    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": str(exc)
            })
        }

    except (BotoCoreError, ClientError):
        logger.exception("AWS error during registration")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Registration service unavailable"
            })
        }

    except Exception:
        logger.exception("Unexpected registration error")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Internal server error"
            })
        }
