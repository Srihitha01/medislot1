# Milestone 5 — Concurrency and Doctor Cancellation

## Booking concurrency
A successful conditional write atomically increments `bookedCount`,
`pendingReservations`, and `version`. It requires an OPEN slot, remaining
capacity, a future start time, and the version observed by the request.
`BookingConditionalConflicts` counts conditional write failures;
`BookingConflictRetries` records retries. Retry attempts remain bounded by
`BOOKING_MAX_ATTEMPTS`.

After the RDS appointment commit, the request decrements
`pendingReservations` and increments `version`. If RDS persistence fails, the
compensation path decrements `bookedCount` and `pendingReservations` and
increments `version`.

## Doctor cancellation
Doctor cancellation first changes the slot from OPEN to CANCELLING. New
reservations require OPEN and are rejected after this transition. The handler
waits for `pendingReservations` to reach zero, locks active RDS appointments,
cancels them, and writes 100% DOCTOR_FULL refund ledger entries in one RDS
transaction. It then changes the slot to CANCELLED and publishes SlotCancelled, one AppointmentCancelled event, and one
SlotSeatFreed event per affected appointment. The SlotSeatFreed payload uses
`availableSeatsIncremented: 0` and `slotStatus: CANCELLED`: the patient's
reservation is released, but the slot is not reopened for booking.

## Recovery
The drain timeout is `SLOT_CANCELLATION_DRAIN_SECONDS` (default 20 seconds).
If it expires, the slot remains CANCELLING and rejects new bookings. If a Lambda
dies after incrementing pendingReservations, or after RDS commits but before
DynamoDB finalization, reconciliation is required. Compare active RDS
appointments with DynamoDB bookedCount and pendingReservations before repairing
the slot. Do not reopen CANCELLING slots without reconciliation.

## Metrics
- BookingConflictRetries
- BookingConditionalConflicts
- CancellationsByTier

## Live test
`scripts/test_booking_concurrency.py` uses a 20-party Barrier to release all
workers together. It requires exactly 3 successes and 17 HTTP 409 conflicts,
replays a successful idempotency key, and cancels the test appointments.
