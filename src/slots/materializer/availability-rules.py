"""MediSlot shared availability rules.

Intervals are half-open [start, end): 09:00-10:00 and 10:00-11:00
are adjacent and valid. Weekdays use ISO numbering (Monday=1).
"""
from datetime import date, datetime, time, timedelta

VALID_TYPES = {"ADD", "OVERRIDE", "CLOSED"}


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
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


def effective_windows(patterns, exceptions, blackouts, target_date):
    """Resolve one consultation type's effective schedule for a date.

    Exception records are dictionaries with exception_type, start_time,
    end_time and exception_date. Multiple ADD/OVERRIDE rows represent
    multiple sessions. A CLOSED record must be the only exception that day.
    """
    day = as_date(target_date)
    if day in {as_date(x) for x in (blackouts or [])}:
        return []

    rows = [r for r in (exceptions or []) if as_date(r["exception_date"]) == day]
    kinds = {str(r["exception_type"]).upper() for r in rows}
    if kinds - VALID_TYPES:
        raise ValueError(f"unknown exception type: {sorted(kinds - VALID_TYPES)}")
    if "CLOSED" in kinds:
        if len(kinds) != 1 or len(rows) != 1:
            raise ValueError("CLOSED cannot be combined with other exceptions")
        if rows[0].get("start_time") is not None or rows[0].get("end_time") is not None:
            raise ValueError("CLOSED must not contain times")
        return []

    overrides = [r for r in rows if str(r["exception_type"]).upper() == "OVERRIDE"]
    additions = [r for r in rows if str(r["exception_type"]).upper() == "ADD"]
    if overrides:
        base = [(r["start_time"], r["end_time"]) for r in overrides]
    else:
        base = [(r["start_time"], r["end_time"]) for r in (patterns or [])
                if int(r["day_of_week"]) == day.isoweekday()]
    extra = [(r["start_time"], r["end_time"]) for r in additions]
    return validate_windows(base + extra, "Effective schedule")


def validate_doctor_day(schedules, target_date):
    """Validate effective windows across all active consultation types.

    Each schedule requires user_id, consultation_type_id, patterns,
    exceptions, blackouts; is_active is optional and defaults true.
    """
    day = as_date(target_date)
    intervals = []
    for s in schedules:
        if not s.get("is_active", True):
            continue
        for start, end in effective_windows(
            s.get("patterns", []), s.get("exceptions", []),
            s.get("blackouts", []), day
        ):
            start, end = window(start, end)
            intervals.append((start, end, str(s["user_id"]),
                              int(s["consultation_type_id"])))
    intervals.sort(key=lambda x: (x[0], x[1]))
    for i, current in enumerate(intervals):
        for other in intervals[i + 1:]:
            if other[0] >= current[1]:
                break
            if current[2] == other[2] and current[3] != other[3]:
                raise ValueError(
                    f"Consultation types {current[3]} and {other[3]} overlap on {day}"
                )
    return True


def validate_exception_payload(kind, start=None, end=None):
    kind = str(kind).upper()
    if kind not in VALID_TYPES:
        raise ValueError("type must be ADD, OVERRIDE or CLOSED")
    if kind == "CLOSED":
        if start is not None or end is not None:
            raise ValueError("CLOSED must not include startTime/endTime")
        return kind, None, None
    if start is None or end is None:
        raise ValueError(f"{kind} requires startTime and endTime")
    start, end = window(start, end)
    return kind, start, end
