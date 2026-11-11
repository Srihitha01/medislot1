import unittest
from datetime import time

from src.slots.materializer.availability_rules import (
    effective_windows,
    validate_doctor_day,
)


class AvailabilityRuleTests(unittest.TestCase):
    def test_weekly_multiple_sessions(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"},
            {"day_of_week": 1, "start_time": "14:00", "end_time": "17:00"},
        ]
        self.assertEqual(
            len(effective_windows(patterns, [], "2026-11-09")), 2
        )

    def test_full_day_blackout_overrides_weekly_pattern(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}
        ]
        blackouts = [
            {"blackout_date": "2026-11-09", "is_full_day": 1}
        ]
        self.assertEqual(
            effective_windows(patterns, blackouts, "2026-11-09"), []
        )

    def test_timed_blackout_does_not_close_the_whole_day(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}
        ]
        blackouts = [
            {"blackout_date": "2026-11-09", "is_full_day": 0,
             "start_time": "10:00", "end_time": "11:00"}
        ]
        self.assertEqual(
            effective_windows(patterns, blackouts, "2026-11-09"),
            [(time(9, 0), time(12, 0))]
        )

    def test_cross_consultation_type_overlap_is_rejected(self):
        schedules = [
            {"user_id": "21", "consultation_type_id": 1, "patterns": [
                {"day_of_week": 1, "start_time": "09:00", "end_time": "11:00"}]},
            {"user_id": "21", "consultation_type_id": 2, "patterns": [
                {"day_of_week": 1, "start_time": "10:00", "end_time": "12:00"}]},
        ]
        with self.assertRaises(ValueError):
            validate_doctor_day(schedules, "2026-11-09")

    def test_adjacent_windows_are_allowed(self):
        patterns = [
            {"day_of_week": 1, "start_time": "09:00", "end_time": "10:00"},
            {"day_of_week": 1, "start_time": "10:00", "end_time": "11:00"},
        ]
        self.assertEqual(
            len(effective_windows(patterns, [], "2026-11-09")), 2
        )


if __name__ == "__main__":
    unittest.main()
