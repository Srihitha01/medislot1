import unittest
from datetime import time

from src.slots.materializer.availability_rules import (
    effective_windows, validate_doctor_day, validate_exception_payload,
    validate_windows,
)


class AvailabilityRulesTests(unittest.TestCase):
    def test_multiple_weekly_sessions_with_break(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"},
            {"day_of_week": 1, "start_time": "14:00", "end_time": "17:00"},
        ]
        self.assertEqual(len(effective_windows(patterns, [], [], "2026-11-09")), 2)

    def test_multiple_override_rows_are_all_kept(self):
        exceptions = [
            {"exception_date": "2026-11-09", "exception_type": "OVERRIDE",
             "start_time": "09:00", "end_time": "11:00"},
            {"exception_date": "2026-11-09", "exception_type": "OVERRIDE",
             "start_time": "14:00", "end_time": "16:00"},
        ]
        self.assertEqual(
            effective_windows([], exceptions, [], "2026-11-09"),
            [(time(9), time(11)), (time(14), time(16))],
        )

    def test_add_appends_to_weekly_schedule(self):
        patterns = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]
        exceptions = [{"exception_date": "2026-11-09", "exception_type": "ADD",
                       "start_time": "14:00", "end_time": "16:00"}]
        self.assertEqual(len(effective_windows(patterns, exceptions, [], "2026-11-09")), 2)

    def test_add_appends_to_override_schedule(self):
        exceptions = [
            {"exception_date": "2026-11-09", "exception_type": "OVERRIDE",
             "start_time": "09:00", "end_time": "11:00"},
            {"exception_date": "2026-11-09", "exception_type": "ADD",
             "start_time": "14:00", "end_time": "16:00"},
        ]
        self.assertEqual(len(effective_windows([], exceptions, [], "2026-11-09")), 2)

    def test_override_replaces_weekly(self):
        patterns = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]
        exceptions = [{"exception_date": "2026-11-09", "exception_type": "OVERRIDE",
                       "start_time": "13:00", "end_time": "16:00"}]
        self.assertEqual(
            effective_windows(patterns, exceptions, [], "2026-11-09"),
            [(time(13), time(16))],
        )

    def test_closed_and_blackout_return_no_windows(self):
        closed = [{"exception_date": "2026-11-09", "exception_type": "CLOSED",
                   "start_time": None, "end_time": None}]
        self.assertEqual(effective_windows([], closed, [], "2026-11-09"), [])
        self.assertEqual(effective_windows([], [], ["2026-11-09"], "2026-11-09"), [])

    def test_overlapping_windows_rejected(self):
        with self.assertRaises(ValueError):
            validate_windows([("09:00", "11:00"), ("10:00", "12:00")])

    def test_adjacent_windows_allowed(self):
        self.assertEqual(
            len(validate_windows([("09:00", "10:00"), ("10:00", "11:00")])), 2
        )

    def test_cross_consultation_overlap_rejected(self):
        schedules = [
            {"user_id": "doctor-1", "consultation_type_id": 1,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
            {"user_id": "doctor-1", "consultation_type_id": 2,
             "patterns": [{"day_of_week": 1, "start_time": "11:00", "end_time": "13:00"}]},
        ]
        with self.assertRaises(ValueError):
            validate_doctor_day(schedules, "2026-11-09")

    def test_other_doctors_can_overlap(self):
        schedules = [
            {"user_id": "doctor-1", "consultation_type_id": 1,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
            {"user_id": "doctor-2", "consultation_type_id": 2,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
        ]
        self.assertTrue(validate_doctor_day(schedules, "2026-11-09"))

    def test_inactive_consultation_type_is_ignored(self):
        schedules = [
            {"user_id": "doctor-1", "consultation_type_id": 1, "is_active": True,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
            {"user_id": "doctor-1", "consultation_type_id": 2, "is_active": False,
             "patterns": [{"day_of_week": 1, "start_time": "10:00", "end_time": "13:00"}]},
        ]
        self.assertTrue(validate_doctor_day(schedules, "2026-11-09"))

    def test_closed_payload_rejects_times(self):
        with self.assertRaises(ValueError):
            validate_exception_payload("CLOSED", "09:00", "10:00")


if __name__ == "__main__":
    unittest.main()
