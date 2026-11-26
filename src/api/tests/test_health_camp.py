_import pytest

from app import _normalise_consultation_name, _validate_consultation_rules


@pytest.mark.parametrize("capacity", [2, 3, 9, 10])
def test_health_camp_accepts_2_to_10(capacity):
    _validate_consultation_rules("health camp session", 60, capacity)


@pytest.mark.parametrize("capacity", [1, 11, 50])
def test_health_camp_rejects_capacity_outside_2_to_10(capacity):
    with pytest.raises(ValueError, match="between 2 and 10"):
        _validate_consultation_rules("health camp session", 60, capacity)


@pytest.mark.parametrize("duration", [30, 59, 61, 90])
def test_health_camp_must_be_60_minutes(duration):
    with pytest.raises(ValueError, match="60 minutes"):
        _validate_consultation_rules("health camp session", duration, 5)


def test_name_matching_ignores_case_and_extra_spaces():
    assert _normalise_consultation_name("  Health   Camp SESSION ") == "health camp session"
    with pytest.raises(ValueError):
        _validate_consultation_rules("  Health   Camp SESSION ", 60, 1)


def test_other_consultation_types_are_unchanged():
    _validate_consultation_rules("standard", 30, 1)
    _validate_consultation_rules("video", 45, 20)
