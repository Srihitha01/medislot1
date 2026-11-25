# Patient cancellation, refund ledger and SES notifications

## Implemented
- Patient-only `POST /appointments/{appointmentId}/cancel`, scoped to the authenticated patient.
- RDS row lock and transaction: validates active/future appointment, updates status, and inserts one refund ledger entry.
- Approved refund tiers: strictly >48h 100%; 24h through 48h 75%; 2h through <24h 50%; less than 2h 0%.
- After RDS commit, attempts to release one DynamoDB seat, publishes `AppointmentCancelled`, and emits `CancellationsByTier`.
- Booking and cancellation notifications are persisted in `patient_notifications`, exposed through `GET /patients/me/notifications`.
- Transactional email is sent directly to the patient's registered `users.email` through Amazon SES. There is no SNS topic or SNS subscription confirmation flow.
- Patient dashboard has a Cancel appointment action and a live Notifications panel.

## SES setup required
1. In Amazon SES in `ap-south-1`, verify the sender email address or sender domain.
2. Add the verified sender address as the SSM String parameter `/medislot/dev/notifications/from-email` (and corresponding parameter for each environment). Do not use a recipient's email as the sender.
3. If the SES account is still in the sandbox, it can send only to verified recipient addresses (or the SES mailbox simulator). Request production access for transactional email before sending to arbitrary registered patient addresses.
4. The API role receives `ses:SendEmail` on identities in this AWS account and `ssm:GetParameter` on the sender parameter.
5. Email delivery is attempted after the RDS notification is saved. A send failure is logged and does not roll back a booking/cancellation. The API log records the SES MessageId when SES accepts the message; acceptance is not proof of final inbox delivery.

## Database migration
Apply `database/migrations/20261125_patient_notifications.sql` to the existing RDS database using the existing migration process. It creates `patient_notifications`. Do not drop/recreate existing tables.

## No payment gateway
`refund_ledger` records the calculated refund amount only. No money is transferred. The API returns `paymentStatus=LEDGER_ONLY_NO_PAYMENT_GATEWAY`.

## Limitations
- Seat release happens after the RDS transaction commits. If the DynamoDB update fails, the cancellation and ledger remain committed and `SLOT_SEAT_RELEASE_FAILED` is logged; reconcile slot capacity before further bookings.
- Doctor-initiated cancellation of an entire slot and full refunds for all affected patients remain a separate workflow.
- Validate live booking, cancellation, email and refund-boundary behavior in the deployed dev environment before production use.
