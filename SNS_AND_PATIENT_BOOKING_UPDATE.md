# MediSlot update: SNS notifications and patient booking rules

## Included changes

### Patient booking rules
- Maximum 3 active appointments across all doctors.
- Maximum 1 active appointment per doctor for each patient, regardless of appointment date/time.
- Only `booked` and `confirmed` statuses count.
- RDS `users` row lock serializes a patient's concurrent booking attempts before the checks and slot reservation.
- Rejections return HTTP 409 with a rule-specific explanation. Existing DynamoDB slot-capacity protection and idempotency behavior are retained.

### Email notifications
- Replaced SES with one standard SNS topic per environment.
- Each patient's registered email is subscribed with a `patientId` message-attribute filter.
- SNS confirmation is required before the email endpoint receives notifications.
- Registration still succeeds if subscription setup fails; the failure is logged.
- The API repeats the subscription request before notification publishing to support existing patients and changed email addresses.
- Existing RDS in-app notification persistence is retained.
- Added an SNS interface VPC endpoint, preserving the private-subnet/no-NAT design.

## Files changed
- `src/api/app.py`
- `src/auth/register/register.py`
- `cloudformation/api-stack.yaml`
- `cloudformation/authentication.yaml`
- `cloudformation/iam-stack.yaml`
- `cloudformation/network-stack.yaml`
- `.github/workflows/deploy.yaml`
- `PATIENT_EMAIL_NOTIFICATIONS_SETUP.md`
- `PATIENT_CANCELLATION_REFUNDS_NOTIFICATIONS.md`
- `PATIENT_BOOKING_RULES.md`

## Deployment order
1. Update the existing IAM stack once using administrator-authorized AWS CLI credentials. The workflow only verifies the IAM stack because it contains the GitHub OIDC deployment role. Follow the PowerShell instructions in `PATIENT_EMAIL_NOTIFICATIONS_SETUP.md`.
2. Commit and push the updated project to a branch matched by the existing workflow (`feature/**` or `main`).
3. The workflow updates the network stack (SNS VPC endpoint), authentication stack (registration Lambda SNS configuration), and API stack (SNS topic and application code).
4. Confirm a test patient's SNS email subscription using the confirmation email.
5. Test booking limits, cancellation/rebooking, and per-patient notification routing in the dev environment.

## Validation performed
- Python source compilation completed successfully.
- The edited CloudFormation templates parsed as YAML.
- Unit tests could not be executed in this packaging environment because the environment does not include the project's runtime dependencies (`bcrypt` and `pymysql`); run the repository workflow's test steps after deployment packaging.
