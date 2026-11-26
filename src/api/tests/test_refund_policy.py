import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app
from app import _calculate_refund

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def default_policy(monkeypatch):
    """Unit tests must not call SSM: use the approved defaults unless a test overrides."""
    app._REFUND_POLICY_CACHE.clear()
    monkeypatch.setattr(app, "_parameter", lambda *a, **k: json.dumps(app._DEFAULT_REFUND_POLICY))
    yield
    app._REFUND_POLICY_CACHE.clear()


def _refund(delta):
    return _calculate_refund(Decimal("100.00"), NOW + delta, NOW)[:3]


def test_refund_above_48_hours_is_100_percent():
    assert _refund(timedelta(hours=48, seconds=1)) == ("FULL", 100, Decimal("100.00"))


def test_refund_at_exactly_48_hours_is_75_percent():
    assert _refund(timedelta(hours=48)) == ("PARTIAL_75", 75, Decimal("75.00"))


def test_refund_just_above_24_hours_is_75_percent():
    assert _refund(timedelta(hours=24, seconds=1)) == ("PARTIAL_75", 75, Decimal("75.00"))


def test_refund_at_exactly_24_hours_is_50_percent():
    assert _refund(timedelta(hours=24)) == ("PARTIAL_50", 50, Decimal("50.00"))


def test_refund_at_exactly_2_hours_is_50_percent():
    assert _refund(timedelta(hours=2)) == ("PARTIAL_50", 50, Decimal("50.00"))


def test_refund_below_2_hours_is_zero():
    assert _refund(timedelta(hours=2, seconds=-1)) == ("NONE", 0, Decimal("0.00"))


def test_refund_at_start_instant_is_zero():
    assert _refund(timedelta(0)) == ("NONE", 0, Decimal("0.00"))


def test_windows_come_from_ssm(monkeypatch):
    custom = [
        {"tier": "FULL", "minimumHours": 72, "refundPercent": 100},
        {"tier": "HALF", "minimumHours": 12, "refundPercent": 50},
        {"tier": "NONE", "minimumHours": 0, "refundPercent": 0, "inclusive": True},
    ]
    monkeypatch.setattr(app, "_parameter", lambda *a, **k: json.dumps(custom))
    app._REFUND_POLICY_CACHE.clear()
    assert _refund(timedelta(hours=73))[:2] == ("FULL", 100)
    assert _refund(timedelta(hours=30))[:2] == ("HALF", 50)
    assert _refund(timedelta(hours=5))[:2] == ("NONE", 0)


def test_invalid_ssm_policy_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(app, "_parameter", lambda *a, **k: "not json")
    app._REFUND_POLICY_CACHE.clear()
    assert _refund(timedelta(hours=49))[:2] == ("FULL", 100)
