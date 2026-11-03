import unittest
from datetime import time

from src.slots.materializer.availability_rules import (
    effective_windows,
    validate_doctor_day,
    validate_exception_payload,
)


class ApiAvailabilityRuleTests(unittest.TestCase):
    def test_weekly_multiple_sessions(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"},
            {"day_of_week": 1, "start_time": "14:00", "end_time": "17:00"},
        ]
        self.assertEqual(len(effective_windows(patterns, [], [], "2026-11-09")), 2)

    def test_blackout_overrides_weekly_and_exceptions(self):
        patterns = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]
        exceptions = [{"exception_date": "2026-11-09", "exception_type": "ADD",
                       "start_time": "14:00", "end_time": "16:00"}]
        self.assertEqual(effective_windows(patterns, exceptions, ["2026-11-09"], "2026-11-09"), [])

    def test_cross_consultation_type_overlap_is_rejected(self):
        schedules = [
            {"user_id": "21", "consultation_type_id": 1, "patterns": [
                {"day_of_week": 1, "start_time": "09:00", "end_time": "11:00"}]},
            {"user_id": "21", "consultation_type_id": 2, "patterns": [
                {"day_of_week": 1, "start_time": "10:00", "end_time": "12:00"}]},
        ]
        with self.assertRaises(ValueError):
            validate_doctor_day(schedules, "2026-11-09")

    def test_closed_exception_cannot_have_times(self):
        with self.assertRaises(ValueError):
            validate_exception_payload("CLOSED", "09:00", "10:00")


if __name__ == "__main__":
    unittest.main()
