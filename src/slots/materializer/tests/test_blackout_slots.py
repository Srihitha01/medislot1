import sys
import types
import unittest
from datetime import date

try:
    import pymysql  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("pymysql")
    stub.cursors = types.SimpleNamespace(DictCursor=object)
    stub.connect = lambda **kwargs: None
    sys.modules["pymysql"] = stub

from src.slots.materializer import materializer


def _schedule(blackouts):
    return {
        "user_id": 4,
        "consultation_type_id": 5,
        "duration_min": 30,
        "capacity": 1,
        "timezone": "Asia/Kolkata",
        "is_active": True,
        "patterns": [{"day_of_week": 2, "start_time": "09:00:00", "end_time": "10:00:00"}],
        "exceptions": [],
        "blackouts": blackouts,
    }


class BlackoutSlotTests(unittest.TestCase):
    def test_blackout_day_generates_blackout_slots(self):
        slots = materializer._schedule_slots_for_day(_schedule([date(2026, 11, 17)]), date(2026, 11, 17))
        self.assertEqual(len(slots), 2)
        self.assertTrue(all(s["status"] == "BLACKOUT" for s in slots))

    def test_normal_day_generates_open_slots(self):
        slots = materializer._schedule_slots_for_day(_schedule([date(2026, 11, 17)]), date(2026, 11, 24))
        self.assertEqual(len(slots), 2)
        self.assertTrue(all(s["status"] == "OPEN" for s in slots))


if __name__ == "__main__":
    unittest.main()