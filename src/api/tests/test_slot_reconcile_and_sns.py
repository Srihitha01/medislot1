"""Cancelled seats must reappear, and SNS notifications must not re-subscribe."""
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import app  # noqa: E402


def _iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.fixture
def slots_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-south-1")
        table = ddb.create_table(
            TableName="slots",
            KeySchema=[{"AttributeName": "slotId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "slotId", "AttributeType": "S"},
                {"AttributeName": "doctorId", "AttributeType": "S"},
                {"AttributeName": "startUtc", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "gsi-doctor-time",
                "KeySchema": [
                    {"AttributeName": "doctorId", "KeyType": "HASH"},
                    {"AttributeName": "startUtc", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _put_slot(table, slot_id, booked, updated_minutes_ago):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    table.put_item(Item={
        "slotId": slot_id, "doctorId": "7", "startUtc": _iso(start),
        "endUtc": _iso(start + timedelta(minutes=30)), "capacity": 1,
        "bookedCount": booked, "status": "OPEN",
        "updatedAt": _iso(datetime.now(timezone.utc) - timedelta(minutes=updated_minutes_ago)),
    })


def _run(monkeypatch, table, active_counts):
    monkeypatch.setattr(app, "_table", lambda name: table)
    monkeypatch.setattr(app, "_active_booking_counts", lambda doctor_id, now: active_counts)
    result = app._public_doctor_slots(7, {"queryStringParameters": {}})
    return json.loads(result["body"])


def test_cancelled_seat_comes_back_even_if_dynamodb_was_not_updated(slots_table, monkeypatch):
    _put_slot(slots_table, "cancelled-slot", booked=1, updated_minutes_ago=10)
    body = _run(monkeypatch, slots_table, active_counts={})  # RDS: no active appointment
    assert [i["slotId"] for i in body["items"]] == ["cancelled-slot"]
    assert body["items"][0]["freeSeats"] == 1
    assert int(slots_table.get_item(Key={"slotId": "cancelled-slot"})["Item"]["bookedCount"]) == 0


def test_really_booked_slot_stays_hidden(slots_table, monkeypatch):
    _put_slot(slots_table, "booked-slot", booked=1, updated_minutes_ago=10)
    body = _run(monkeypatch, slots_table, active_counts={"booked-slot": 1})
    assert body["items"] == []


def test_booking_in_flight_is_not_repaired(slots_table, monkeypatch):
    _put_slot(slots_table, "fresh-slot", booked=1, updated_minutes_ago=0)
    body = _run(monkeypatch, slots_table, active_counts={})
    assert body["items"] == []
    assert int(slots_table.get_item(Key={"slotId": "fresh-slot"})["Item"]["bookedCount"]) == 1


def test_sns_notification_subscribes_once_and_uses_real_newlines(monkeypatch):
    calls = {"subscribe": 0, "published": []}

    class FakeSns:
        def __init__(self):
            self.subscribed = []

        def list_subscriptions_by_topic(self, **kwargs):
            return {"Subscriptions": [{"Endpoint": e} for e in self.subscribed]}

        def subscribe(self, **kwargs):
            calls["subscribe"] += 1
            self.subscribed.append(kwargs["Endpoint"])
            return {"SubscriptionArn": "pending confirmation"}

        def publish(self, **kwargs):
            calls["published"].append(kwargs)
            return {"MessageId": "m-1"}

    fake = FakeSns()
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:ap-south-1:1:t")
    monkeypatch.setattr(app, "_sns_client", lambda: fake)

    class Conn:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **k):
            pass
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(app, "_db_connection", lambda: Conn())
    for _ in range(2):
        app._notify_patient(3, 9, "APPOINTMENT_BOOKED", "Appointment booked", "Confirmed.", "Patient@Example.com")
    assert calls["subscribe"] == 1
    assert len(calls["published"]) == 2
    message = calls["published"][0]["Message"]
    assert "\n\n" in message and "\\n" not in message
    assert calls["published"][0]["MessageAttributes"]["patientId"]["StringValue"] == "3"


def test_slot_with_active_appointment_but_zero_count_is_hidden_and_repaired(slots_table, monkeypatch):
    _put_slot(slots_table, "held-slot", booked=0, updated_minutes_ago=10)
    body = _run(monkeypatch, slots_table, active_counts={"held-slot": 1})
    assert body["items"] == []
    assert int(slots_table.get_item(Key={"slotId": "held-slot"})["Item"]["bookedCount"]) == 1


def test_naive_mysql_datetime_is_sent_as_utc_with_z():
    from datetime import datetime
    assert app._utc_z(datetime(2026, 12, 7, 15, 20)) == "2026-12-07T15:20:00Z"
