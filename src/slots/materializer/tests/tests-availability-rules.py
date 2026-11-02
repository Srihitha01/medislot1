import unittest
from datetime import date, time
from src.slots.materializer.availability_rules import (
    effective_windows, validate_doctor_day, validate_exception_payload,
    validate_windows,
)

class AvailabilityTests(unittest.TestCase):
    def test_multiple_sessions_and_break(self):
        rows = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"},
                {"day_of_week": 1, "start_time": "14:00", "end_time": "17:00"}]
        self.assertEqual(len(effective_windows(rows, [], [], "2026-11-09")), 2)

    def test_adjacent_sessions_allowed(self):
        self.assertEqual(len(validate_windows([("09:00", "10:00"), ("10:00", "11:00")])), 2)

    def test_same_type_overlap_rejected(self):
        with self.assertRaises(ValueError):
            validate_windows([("09:00", "11:00"), ("10:00", "12:00")])

    def test_add_appends(self):
        patterns = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]
        exceptions = [{"exception_date": "2026-11-09", "exception_type": "ADD",
                       "start_time": "14:00", "end_time": "16:00"}]
        self.assertEqual(len(effective_windows(patterns, exceptions, [], "2026-11-09")), 2)

    def test_override_replaces(self):
        patterns = [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]
        exceptions = [{"exception_date": "2026-11-09", "exception_type": "OVERRIDE",
                       "start_time": "13:00", "end_time": "16:00"}]
        self.assertEqual(effective_windows(patterns, exceptions, [], "2026-11-09"),
                         [(time(13), time(16))])

    def test_closed(self):
        ex = [{"exception_date": "2026-11-09", "exception_type": "CLOSED",
               "start_time": None, "end_time": None}]
        self.assertEqual(effective_windows([], ex, [], "2026-11-09"), [])

    def test_blackout_wins(self):
        self.assertEqual(effective_windows([], [], ["2026-11-09"], "2026-11-09"), [])

    def test_cross_consultation_overlap_rejected(self):
        schedules = [
            {"user_id": "doctor-1", "consultation_type_id": 1,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
            {"user_id": "doctor-1", "consultation_type_id": 2,
             "patterns": [{"day_of_week": 1, "start_time": "11:00", "end_time": "13:00"}]},
        ]
        with self.assertRaises(ValueError):
            validate_doctor_day(schedules, "2026-11-09")

    def test_different_doctors_can_overlap(self):
        schedules = [
            {"user_id": "doctor-1", "consultation_type_id": 1,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
            {"user_id": "doctor-2", "consultation_type_id": 2,
             "patterns": [{"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}]},
        ]
        self.assertTrue(validate_doctor_day(schedules, "2026-11-09"))

    def test_closed_payload_rejects_times(self):
        with self.assertRaises(ValueError):
            validate_exception_payload("CLOSED", "09:00", "10:00")

if __name__ == "__main__":
    unittest.main()
