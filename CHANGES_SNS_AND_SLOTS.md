# Changes: SNS email notifications and cancelled-slot visibility

## Cancelled slots
- `src/api/app.py` `_public_doctor_slots`: seat availability is now checked against the
  appointments in RDS. If DynamoDB still counts a seat for a cancelled appointment
  (older than 2 minutes), the seat is shown as free and DynamoDB is repaired.
- `_cancel_patient_appointment`: the seat release is retried 3 times and logs
  `SLOT_SEAT_RELEASED` / `SLOT_SEAT_RELEASE_FAILED` / `SLOT_SEAT_RELEASE_GAVE_UP`.
- `patient_dashboard.html`: the doctor's slot list refreshes after a cancel and when
  returning to "Find a doctor".

## SNS notifications (replaces SES)
- Email text now has real line breaks (it contained a literal `\n`).
- `_notify_patient` only subscribes an address that has no subscription yet, so
  patients are not sent a new AWS confirmation email on every booking.
- `iam-stack.yaml`: app router role also gets `sns:ListSubscriptionsByTopic`.
- Dashboard wording says SNS and tells patients to confirm the first AWS email.

## Tests
- `src/api/tests/test_slot_reconcile_and_sns.py` (4 tests, all pass).
- Existing failures, unchanged by this work: `test_cross_patient_id_forbidden` and
  `test_refund_at_exactly_24_hours_is_50_percent`.

## Round 2 (from the DynamoDB scan and screenshots)
- Appointment and notification times were shown 5h30 early: MySQL returns UTC
  datetimes without a zone and the browser read them as local time (a 20:50 IST
  appointment appeared as 03:20 pm). The API now sends them with a `Z`, and the
  dashboard also treats zone-less timestamps as UTC.
- Slot list repairs DynamoDB in both directions. If RDS has an active appointment
  that DynamoDB does not count, the slot is hidden and the count is corrected
  (prevents double booking).
