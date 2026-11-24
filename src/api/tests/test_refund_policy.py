from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import _calculate_refund


def test_refund_above_48_hours_is_100_percent():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tier, percent, amount, _ = _calculate_refund(Decimal("100.00"), now + timedelta(hours=48, seconds=1), now)
    assert (tier, percent, amount) == ("FULL", 100, Decimal("100.00"))


def test_refund_at_exactly_48_hours_is_75_percent():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tier, percent, amount, _ = _calculate_refund(Decimal("100.00"), now + timedelta(hours=48), now)
    assert (tier, percent, amount) == ("PARTIAL_75", 75, Decimal("75.00"))


def test_refund_at_exactly_24_hours_is_50_percent():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tier, percent, amount, _ = _calculate_refund(Decimal("100.00"), now + timedelta(hours=24), now)
    assert (tier, percent, amount) == ("PARTIAL_50", 50, Decimal("50.00"))


def test_refund_at_exactly_2_hours_is_50_percent():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tier, percent, amount, _ = _calculate_refund(Decimal("100.00"), now + timedelta(hours=2), now)
    assert (tier, percent, amount) == ("PARTIAL_50", 50, Decimal("50.00"))


def test_refund_below_2_hours_is_zero():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tier, percent, amount, _ = _calculate_refund(Decimal("100.00"), now + timedelta(hours=2, seconds=-1), now)
    assert (tier, percent, amount) == ("NONE", 0, Decimal("0.00"))
