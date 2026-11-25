# Patient cancellation, refund ledger and SNS notifications

## Implemented
- Patient-only `POST /appointments/{appointmentId}/cancel`, scoped to the authenticated patient.
- RDS row lock and transaction: validates active/future appointment, updates status, and inserts one refund ledger entry.
- Approved refund tiers: strictly >48h 100%; 24h through 48h 75%; 2h through <24h 50%; less than 2h 0%.
- After RDS commit, attempts to release one DynamoDB seat, publishes `AppointmentCancelled`, and emits `CancellationsByTier`.
- Booking and cancellation notifications are persisted in `patient_notifications`, exposed through `GET /patients/me/notifications`.
- Patient email notifications are published through the shared Amazon SNS standard topic. Each email subscription has a message-attribute filter matching that patient's numeric `patientId`.
- Patient email subscriptions require confirmation through the SNS confirmation email. The patient must confirm before SNS sends appointment notifications to that address.
- Patient dashboard has a Cancel appointment action and a live Notifications panel.

## SNS notification setup
1. CloudFormation creates `medislot-<environment>-patient-notifications` in the API stack.
2. The registration Lambda subscribes a newly registered patient's email address with a `patientId` filter. SNS sends the confirmation email.
3. The application Lambda saves the in-app notification to RDS and publishes to SNS with the matching `patientId` message attribute.
4. SNS filter policies route the notification only to the matching patient's confirmed email subscription.
5. SNS publish failures are logged and do not roll back a committed booking/cancellation. `SNS_EMAIL_PUBLISH_ACCEPTED` means SNS accepted the publish request, not that the email reached the inbox.
6. The network stack provides an SNS interface VPC endpoint for private Lambda subnets. No NAT Gateway is added.
7. SES identities, SES sender parameters, SES IAM permissions, and the `MEDISLOT_SES_FROM_EMAIL` GitHub secret are not required.

## Database migration
Apply `database/migrations/20261125_patient_notifications.sql` to the existing RDS database using the existing migration process. It creates `patient_notifications`. Do not drop/recreate existing tables.

## No payment gateway
`refund_ledger` records the calculated refund amount only. No money is transferred. The API returns `paymentStatus=LEDGER_ONLY_NO_PAYMENT_GATEWAY`.

## Limitations
- Seat release happens after the RDS transaction commits. If the DynamoDB update fails, the cancellation and ledger remain committed and `SLOT_SEAT_RELEASE_FAILED` is logged; reconcile slot capacity before further bookings.
- Doctor-initiated cancellation of an entire slot and full refunds for all affected patients remain a separate workflow.
- SNS email delivery uses the standard SNS email format; it does not provide custom HTML email templates or a custom sender domain.
- Existing patients who predate SNS setup are subscribed when MediSlot next attempts to send them a notification. They must confirm the subscription.
- Validate live booking, cancellation, email confirmation, notification routing, and refund-boundary behavior in the deployed dev environment before production use.
