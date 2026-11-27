# MediSlot Milestone 5 update

## Changes in this package

1. Added caller-scoped `GET /appointments/{appointmentId}` for patient/doctor owners.
2. Added doctor-only `POST /doctors/me/slots/{slotId}/cancel` with RDS row locking, cancellation of all active appointments on the doctor's slot, a 100% `DOCTOR_CANCEL` refund-ledger entry per patient, slot status update to `CANCELLED`, per-patient AppointmentCancelled event, metric and notification.
3. Patient cancellation now publishes `SlotSeatFreed` only after the DynamoDB seat counter update succeeds. `AppointmentCancelled` remains published after the RDS cancellation/refund transaction.
4. Added `scripts/test_booking_concurrency.py`, a live 20-client test for a deployed API: capacity-3 slot, exactly 3 successes, 17 conflicts, same-key replay returns the original appointment, and successful test bookings are cancelled afterward.
5. Added API test execution to the GitHub Actions validation job and documented the live test prerequisites.
6. Removed the bootstrap IAM scripts from this package; IAM deployment remains through the existing CloudFormation stack/workflow.
7. Removed UUID-based test email generation from the existing live integration test. Database/application IDs remain sequential numeric IDs.

## Verification status

- Python compilation: passed in the packaging environment.
- GitHub Actions YAML syntax: parsed successfully.
- API pytest suite: configured to run in CI, but could not be executed in this packaging environment because the environment cannot download the missing PyMySQL dependency.
- CloudFormation deployment, deployed RDS/DynamoDB behavior, EventBridge delivery, CloudWatch metric publication and the live 20-client test have not been executed here. Run the workflow and then `scripts/test_booking_concurrency.py` against the deployed test environment before marking the mentor checkpoint passed.

## Important payment boundary

Refunds are written to `refund_ledger` in RDS. MediSlot has no connected payment gateway in this implementation, so a ledger refund is not a money transfer.
