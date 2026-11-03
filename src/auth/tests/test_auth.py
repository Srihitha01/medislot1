"""Tests for whole-number user ids (1, 2, 3 ...).

Run from the repository root:
    python -m unittest discover -s src/auth/tests -p "test_*.py" -v
"""

import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DB_USER", "medislot_app")
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import jwt
from src.auth.authorizer import authorizer
from src.auth.register import register
from src.auth.schema_init import schema_init


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_FILES = (
    ROOT / "database" / "schema.sql",
    ROOT / "src" / "auth" / "schema_init" / "schema.sql",
)


# --------------------------------------------------
# SCHEMA FILES
# --------------------------------------------------

class SchemaTests(unittest.TestCase):

    def test_no_uuid_columns_are_left(self):
        for path in SCHEMA_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("CHAR(36)", text, msg=str(path))

    def test_users_table_numbers_its_own_ids(self):
        for path in SCHEMA_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", text,
                msg=str(path)
            )

    def test_every_user_reference_has_the_same_type(self):
        """Foreign keys only work when both columns have the same type."""
        for path in SCHEMA_FILES:
            text = path.read_text(encoding="utf-8")
            columns = re.findall(
                r"^\s+((?:doctor_|patient_)?user_id)\s+([^,\n]+)",
                text, re.MULTILINE
            )

            self.assertGreaterEqual(len(columns), 9, msg=str(path))

            for name, kind in columns:
                self.assertTrue(
                    kind.startswith("BIGINT UNSIGNED"),
                    msg=f"{path.name}: {name} is {kind}"
                )

    def test_refund_ids_are_numbers_too(self):
        for path in SCHEMA_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", text
            )


# --------------------------------------------------
# REGISTRATION
# --------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))

        if "INSERT INTO users" in sql:
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id


class FakeConnection:
    def __init__(self, first_id=0):
        self.executed = []
        self.next_id = first_id
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def ran(self, fragment):
        return [item for item in self.executed if fragment in item[0]]


PATIENT = {
    "role": "patient", "name": "Ravi Kumar", "email": "ravi@example.com",
    "password": "Password123", "dateOfBirth": "1990-05-01",
    "gender": "male", "phone": "9876543210", "city": "Hyderabad",
}

DOCTOR = {
    "role": "doctor", "name": "Asha Rao", "email": "asha@example.com",
    "password": "Password123", "licenseNumber": "KA-1", "specialisationId": 2,
    "hospitalId": 1, "yearsExperience": 8, "qualifications": "MBBS",
    "languages": "English", "timezone": "Asia/Kolkata",
}


class RegistrationTests(unittest.TestCase):

    def register(self, payload, first_id=0):
        conn = FakeConnection(first_id)

        with patch.object(register, "db", return_value=conn):
            result = register.lambda_handler(
                {"body": json.dumps(payload)}, None
            )

        return conn, result["statusCode"], json.loads(result["body"])

    def test_patient_gets_the_number_the_database_assigned(self):
        conn, status, body = self.register(PATIENT, first_id=6)

        self.assertEqual(status, 201)
        self.assertEqual(body["userId"], 7)

        user_sql, user_params = conn.ran("INSERT INTO users")[0]
        self.assertNotIn("user_id", user_sql)
        self.assertEqual(user_params[0], "Ravi Kumar")

        _, patient_params = conn.ran("INSERT INTO patients")[0]
        self.assertEqual(patient_params[0], 7)

    def test_doctor_profile_uses_the_same_number(self):
        conn, status, body = self.register(DOCTOR, first_id=0)

        self.assertEqual(status, 201)
        self.assertEqual(body["userId"], 1)

        _, doctor_params = conn.ran("INSERT INTO doctors")[0]
        self.assertEqual(doctor_params[0], 1)
        self.assertEqual(conn.commits, 1)

    def test_ids_count_up(self):
        ids = []
        conn = FakeConnection()

        for index in range(3):
            payload = dict(PATIENT, email=f"p{index}@example.com")

            with patch.object(register, "db", return_value=conn):
                result = register.lambda_handler(
                    {"body": json.dumps(payload)}, None
                )

            ids.append(json.loads(result["body"])["userId"])

        self.assertEqual(ids, [1, 2, 3])

    def test_id_is_a_number_not_text(self):
        _, _, body = self.register(PATIENT)
        self.assertIsInstance(body["userId"], int)


# --------------------------------------------------
# LOGIN GATE
# --------------------------------------------------

class UserIdCheckTests(unittest.TestCase):

    def run_authorizer(self, subject):
        claims = {
            "sub": subject, "role": "patient", "iat": 100,
            "exp": 9999999999, "jti": "j", "sid": "s",
        }
        active = {
            "Item": {
                "tokenType": "ACTIVE_ACCESS", "jti": "j", "sessionId": "s"
            }
        }
        event = {
            "authorizationToken": "Bearer t",
            "methodArn": "arn:aws:execute-api:r:a:api/dev/GET/x",
        }

        with patch.object(authorizer, "get_secret", return_value="k"), \
                patch.object(authorizer.jwt, "decode", return_value=claims), \
                patch.object(authorizer.table, "get_item", return_value=active):
            return authorizer.lambda_handler(event, None)

    def test_whole_numbers_are_accepted(self):
        for subject in ("1", "2", "101", 7):
            result = self.run_authorizer(subject)
            self.assertEqual(result["context"]["userId"], str(subject))

    def test_old_uuid_tokens_are_rejected(self):
        # "5f3e..." would be read by MySQL as the number 5. It must not pass.
        for subject in (
            "5f3e1c7a-aaaa-bbbb-cccc-1234567890ab",
            "cc70dc34-033d-4b25-93b4-12542fe09496",
        ):
            with self.assertRaises(Exception, msg=subject) as raised:
                self.run_authorizer(subject)
            self.assertEqual(str(raised.exception), "Unauthorized")

    def test_other_odd_ids_are_rejected(self):
        for subject in ("0", "-1", "1.5", "1 OR 1=1", "", "007", "٣"):
            with self.assertRaises(Exception, msg=repr(subject)):
                self.run_authorizer(subject)


# --------------------------------------------------
# SCHEMA INITIALISER
# --------------------------------------------------

class FakeSchemaCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.conn.executed.append(flat)

        if "information_schema.COLUMNS" in flat:
            self.row = (self.conn.user_id_type,) if self.conn.user_id_type else None

    def fetchone(self):
        return self.row


class FakeSchemaConnection:
    def __init__(self, user_id_type):
        self.user_id_type = user_id_type
        self.executed = []
        self.rolled_back = False

    def cursor(self):
        return FakeSchemaCursor(self)

    def escape(self, value):
        return "'x'"

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class SchemaInitTests(unittest.TestCase):

    ENV = {
        "ENVIRONMENT": "dev", "DB_USER": "medislot_app", "DB_NAME": "medislot",
    }

    def invoke(self, event, user_id_type, environment="dev"):
        conn = FakeSchemaConnection(user_id_type)
        env = dict(self.ENV, ENVIRONMENT=environment)

        with patch.dict(os.environ, env), \
                patch.object(schema_init, "get_parameter", return_value="3306"), \
                patch.object(schema_init.pymysql, "connect", return_value=conn):
            result = schema_init.lambda_handler(event, None)

        return conn, result

    def test_table_names_are_read_from_the_schema_file(self):
        names = schema_init._table_names()
        self.assertEqual(names[0], "users")
        self.assertEqual(names[-1], "refund_ledger")
        self.assertIn("appointments", names)

    def test_old_uuid_tables_are_detected(self):
        cursor = FakeSchemaCursor(FakeSchemaConnection("char"))
        self.assertTrue(schema_init._uses_old_uuid_ids(cursor, "medislot"))

        cursor = FakeSchemaCursor(FakeSchemaConnection("bigint"))
        self.assertFalse(schema_init._uses_old_uuid_ids(cursor, "medislot"))

        cursor = FakeSchemaCursor(FakeSchemaConnection(None))
        self.assertFalse(schema_init._uses_old_uuid_ids(cursor, "medislot"))

    def test_normal_run_stops_when_old_tables_exist(self):
        conn, result = self.invoke({"action": "initialize"}, "char")

        self.assertEqual(result["statusCode"], 409)
        self.assertIn("reset", json.loads(result["body"])["message"])
        self.assertTrue(conn.rolled_back)
        self.assertFalse(any("CREATE TABLE" in sql for sql in conn.executed))
        self.assertFalse(any("DROP TABLE" in sql for sql in conn.executed))

    def test_normal_run_works_on_a_new_database(self):
        conn, result = self.invoke({"action": "initialize"}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertTrue(any("CREATE TABLE" in sql for sql in conn.executed))
        self.assertFalse(any("DROP TABLE" in sql for sql in conn.executed))

    def test_normal_run_works_on_integer_tables(self):
        _, result = self.invoke({}, "bigint")
        self.assertEqual(result["statusCode"], 200)

    def test_reset_needs_confirmation(self):
        with self.assertRaises(ValueError):
            self.invoke({"action": "reset"}, "char")

        with self.assertRaises(ValueError):
            self.invoke({"action": "reset", "confirm": "yes"}, "char")

    def test_reset_is_blocked_outside_dev_and_test(self):
        with self.assertRaises(ValueError):
            self.invoke(
                {"action": "reset", "confirm": "DELETE_ALL_DATA"},
                "char", environment="prod"
            )

    def test_reset_drops_children_first_then_recreates(self):
        conn, result = self.invoke(
            {"action": "reset", "confirm": "DELETE_ALL_DATA"}, "char"
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(
            json.loads(result["body"])["message"],
            "Database reset and schema created"
        )

        drops = [s for s in conn.executed if s.startswith("DROP TABLE")]
        self.assertEqual(drops[0], "DROP TABLE IF EXISTS `refund_ledger`")
        self.assertEqual(drops[-1], "DROP TABLE IF EXISTS `users`")

        first_drop = conn.executed.index(drops[0])
        first_create = next(
            i for i, s in enumerate(conn.executed) if "CREATE TABLE" in s
        )
        self.assertLess(first_drop, first_create)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.invoke({"action": "explode"}, None)


if __name__ == "__main__":
    unittest.main()
