import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

# The tests exercise pure scheduling helpers; provide a minimal PyMySQL stub
# when running in a lightweight CI environment without the Lambda dependency.
try:
    import pymysql  # noqa: F401
except ModuleNotFoundError:
    pymysql_stub = types.ModuleType("pymysql")
    pymysql_stub.cursors = types.SimpleNamespace(DictCursor=object)
    pymysql_stub.connect = lambda **kwargs: None
    sys.modules["pymysql"] = pymysql_stub

from src.slots.materializer import materializer


class FakeDynamoTable:
    def __init__(self, items):
        self.items = items
        self.query_args = []
        self.updated = []

    def query(self, **kwargs):
        self.query_args.append(kwargs)
        return {"Items": self.items}

    def update_item(self, **kwargs):
        self.updated.append(kwargs)


class MaterializerSafetyTests(unittest.TestCase):
    def test_weekly_pattern_generates_only_recurring_sessions(self):
        schedule = {
            "user_id": "doctor-1", "consultation_type_id": 4,
            "timezone": "Asia/Kolkata", "duration_min": 60,
            "capacity": 1, "is_active": 1,
            "patterns": [
                {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}
            ],
            "blackouts": [],
        }
        slots = materializer._schedule_slots_for_day(
            schedule, date(2026, 11, 9)
        )
        self.assertEqual(len(slots), 3)
        self.assertEqual(
            [slot["localStartTime"] for slot in slots],
            ["09:00:00", "10:00:00", "11:00:00"],
        )

    def test_reconciliation_query_has_date_envelope_and_does_not_cancel_outside_range(self):
        table = FakeDynamoTable([
            {"slotId": "d#1#old", "doctorId": "d", "startUtc": "2026-11-09T04:00:00Z",
             "localDate": "2026-11-09", "source": "MATERIALIZED",
             "bookedCount": 0, "status": "CANCELLED"},
            {"slotId": "d#1#outside", "doctorId": "d", "startUtc": "2026-11-20T04:00:00Z",
             "localDate": "2026-11-20", "source": "MATERIALIZED",
             "bookedCount": 0, "status": "OPEN"},
            {"slotId": "d#1#booked", "doctorId": "d", "startUtc": "2026-11-09T05:00:00Z",
             "localDate": "2026-11-09", "source": "MATERIALIZED",
             "bookedCount": 1, "status": "OPEN"},
            {"slotId": "d#1#stale", "doctorId": "d", "startUtc": "2026-11-09T06:00:00Z",
             "localDate": "2026-11-09", "source": "MATERIALIZED",
             "bookedCount": 0, "status": "OPEN"},
        ])
        # The current time is before the requested window so the booked item is
        # counted as preserved; cancelled and outside-window items are untouched.
        with patch.object(materializer, "utc_iso", side_effect=lambda value: "2026-11-01T00:00:00Z"):
            cancelled, preserved = materializer._reconcile_window(
                table, "d", date(2026, 11, 9), date(2026, 11, 10), set()
            )
        self.assertEqual(cancelled, 1)
        self.assertEqual(preserved, 1)
        self.assertEqual(len(table.updated), 1)
        self.assertEqual(table.updated[0]["Key"]["slotId"], "d#1#stale")
        self.assertEqual(len(table.query_args), 1)
        expression = table.query_args[0]["KeyConditionExpression"]
        self.assertIsNotNone(expression)


if __name__ == "__main__":
    unittest.main()
