"""MediSlot availability resolution and validation.

Intervals are half-open [start, end). Adjacent sessions are valid.
Weekdays use ISO numbering: Monday=1 ... Sunday=7.
"""
from datetime import date, datetime, time, timedelta



def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def as_time(value):
    if isinstance(value, time):
        result = value
    elif isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        if not 0 <= seconds < 86400:
            raise ValueError("time must be within one day")
        result = time(seconds // 3600, seconds % 3600 // 60, seconds % 60)
    else:
        try:
            result = time.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("time must be HH:MM[:SS]") from exc
    if result.tzinfo is not None:
        raise ValueError("availability times must be local wall-clock times")
    return result


def window(start, end):
    start, end = as_time(start), as_time(end)
    if start >= end:
        raise ValueError("startTime must be earlier than endTime")
    return start, end


def validate_windows(windows, label="Availability"):
    result = sorted((window(a, b) for a, b in windows), key=lambda x: x[0])
    for previous, current in zip(result, result[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"{label} contains overlapping windows")
    return result


def overlaps(a, b):
    a0, a1 = window(*a)
    b0, b1 = window(*b)
    return a0 < b1 and b0 < a1


def _full_day_blackout_date(item):
    """Return a full-day blackout date; timed blackouts are handled per slot."""
    if isinstance(item, dict):
        if not item.get("is_full_day", 1):
            return None
        item = item.get("blackout_date")
    return as_date(item)


def effective_windows(patterns, blackouts, target_date):
    """Resolve recurring weekly patterns for one local date, minus full-day blackouts."""
    day = as_date(target_date)
    blackout_days = {
        blackout_day
        for blackout_day in (_full_day_blackout_date(item) for item in (blackouts or []))
        if blackout_day is not None
    }
    if day in blackout_days:
        return []

    windows = [
        (row["start_time"], row["end_time"])
        for row in (patterns or [])
        if int(row["day_of_week"]) == day.isoweekday()
    ]
    return validate_windows(windows, "Weekly availability")


def validate_doctor_day(schedules, target_date):
    """Reject any overlapping active windows for one doctor, including same-type overlaps."""
    day = as_date(target_date)
    intervals = []
    for schedule in schedules:
        if not schedule.get("is_active", True):
            continue
        doctor_id = str(schedule["user_id"])
        consultation_id = int(schedule["consultation_type_id"])
        windows = effective_windows(
            schedule.get("patterns", []), schedule.get("blackouts", []), day,
        )
        for start, end in windows:
            start, end = window(start, end)
            intervals.append((doctor_id, consultation_id, start, end))

    intervals.sort(key=lambda item: (item[0], item[2], item[3], item[1]))
    for index, current in enumerate(intervals):
        for other in intervals[index + 1:]:
            if other[0] != current[0]:
                if other[0] > current[0]:
                    break
                continue
            if other[2] >= current[3]:
                break
            raise ValueError(
                f"Consultation types {current[1]} and {other[1]} overlap on {day.isoformat()}"
            )
    return True


