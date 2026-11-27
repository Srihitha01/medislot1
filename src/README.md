

## Milestone 5 additions (booking, cancellation and refunds)

- `GET /appointments/{appointmentId}` returns a single appointment only to its owning patient or doctor. A non-owner receives `404` so appointment existence is not disclosed.
- `POST /appointments/{appointmentId}/cancel` remains patient-only. It writes the cancellation and refund ledger in one RDS transaction, releases the DynamoDB seat, publishes `AppointmentCancelled`, and publishes `SlotSeatFreed` only after the seat-release write succeeds.
- `POST /doctors/me/slots/{slotId}/cancel` is doctor-only. It selects the doctor's active appointments for that slot under row locks, rejects started appointments, cancels all affected appointments and writes a 100% `DOCTOR_CANCEL` refund ledger row for each in one RDS transaction, then marks the DynamoDB slot `CANCELLED`. It emits an `AppointmentCancelled` event and patient notification for each affected appointment. This is a ledger record; no payment gateway is connected, so it does not transfer money.
- `CancellationsByTier` includes the `DOCTOR_FULL` tier for doctor-initiated cancellation. `BookingConflictRetries` is emitted by the booking OCC retry path.
- CI runs the API unit/contract test suite and refund boundary tests. For an actual deployed-service contention test, run `scripts/test_booking_concurrency.py` with `MEDISLOT_API_BASE_URL`, `TEST_REGISTRATION_DOMAIN`, and `TEST_REGISTRATION_PASSWORD` set to a controlled test environment. It creates 20 patient accounts, makes 20 parallel attempts against an empty capacity-3 slot, asserts exactly 3 successes and 17 conflicts, replays one successful idempotency key, and cancels the three test appointments.

### Milestone 5 verification boundary

The live contention script requires a deployed AWS environment and test registration credentials; it is intentionally not run automatically on every push because doing so creates real patient accounts and appointments. The CI unit/contract suite is separate from that live AWS verification. A successful CI run and a successful live script run are both required before claiming the mentor's live concurrency checkpoint is passed.
