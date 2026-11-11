# MediSlot — Doctor + Patient Portal Upgrade

This archive extends the uploaded `medislot(7).zip`. It is **source code**, not a deployed AWS system.

## Delivered

- `src/api/app.py`: patient profile CRUD scoped to authorizer `userId`, public doctor search with optional combined name/hospital/specialisation/city filters and pagination, public doctor details/fees, and DynamoDB doctor slot availability.
- `src/doctor/app.py`: doctor and patient logins on **the same Flask/Gunicorn process and EC2 instance**. `/dashboard` remains doctor-only; `/patient` is patient-only.
- `src/doctor/templates/patient_dashboard.html`: responsive hospital-style patient portal with profile editor, search, doctor detail and slot browser.
- `cloudformation/api-stack.yaml`: public API Gateway route definitions for doctor discovery; protected patient routes use the existing authorizer.
- `database/search_indexes.sql`: optional additive indexes, with reasoning below.
- `src/api/tests/test_patient_contract.py`: route authorization and pagination unit checks.
- `scripts/integration_live.py`: **real HTTP integration tests**: register a patient, log in, search, read slots and reject cross-patient access.
- `.github/workflows/deploy.yaml`: live tests run after API and dashboard deployment, requiring explicit test-account secrets.

## Setup / deployment

1. Back up RDS, EC2 and CloudFormation stacks. Review `git diff` against your current deployed revision.
2. Use the existing GitHub Actions workflow and AWS OIDC role; the workflow deploys the updated API Lambda and dashboard ZIP to the existing CloudFormation EC2 stack. **Do not launch a second dashboard EC2 stack.** Confirm the stack is the one currently serving your site.
3. Set GitHub Actions secrets: `AWS_ROLE_ARN`, `MEDISLOT_API_BASE_URL`, `TEST_PATIENT_A_EMAIL`, `TEST_PATIENT_A_PASSWORD`, `TEST_PATIENT_B_EMAIL`, `TEST_PATIENT_B_PASSWORD`, `TEST_REGISTRATION_DOMAIN`, `TEST_REGISTRATION_PASSWORD`. Use isolated non-production test accounts and a controlled email domain. Each CI run creates a test patient account, so plan test-data cleanup.
4. Review `database/search_indexes.sql`; run once from a host with database access and schema-change privileges, after backing up. Example on your DB-connected EC2: `mysql -h "$DB_HOST" -u "$DB_ADMIN_USER" -p medislot < database/search_indexes.sql`. Do **not** put passwords in scripts or logs. Existing indexes cover several queries; inspect `EXPLAIN` before applying additions.
5. Check `https://<api-id>.execute-api.ap-south-1.amazonaws.com/dev/doctors/search?pageSize=10`, then log in to `/patient` as a patient. Existing doctors continue using `/dashboard`.
6. CI live integration tests require the deployment to succeed and both test accounts to exist; they are not mocks. They have **not** been run in this environment.

## API contracts

- `GET|POST|PUT|PATCH|DELETE /patients/me`: patient bearer token required. Delete removes patient profile details, **not** the account or historical bookings. No `/patients/{id}` access is supported.
- `GET /doctors/search?name=&hospital=&specialisation=&city=&page=1&pageSize=10`: public, prefix matching, max 50/page.
- `GET /doctors/{id}`: public, non-sensitive doctor details and active consultation types/fees.
- `GET /doctors/{id}/slots?startFrom=<ISO UTC>&startTo=<ISO UTC>`: public, max 31 days, open slots with positive `freeSeats=capacity-bookedCount`.

## Database index rationale

The existing schema already has `idx_doctors_full_name`, `idx_doctors_specialisation_hospital`, `idx_hospitals_city`, and `idx_hospitals_status_city`. These support prefix doctor-name search, speciality/hospital joins and city filtering respectively. Optional `idx_hospitals_name_search` supports hospital-name prefix filtering; `idx_doctors_specialisation_name` may help speciality + doctor-name searches. Combined filters can use joins and index selection; use `EXPLAIN ANALYZE` with realistic data to verify, since the optimizer may choose other indexes. Search uses `LIKE 'term%'` rather than `'%term%'` so B-tree prefix indexes remain useful.

## Known limits / next hardening

- **No live AWS verification has been performed here.** IAM policies, RDS indexes, EC2 deployment, photo upload, and browser behavior must be checked after deployment.
- Earlier S3 photo-finalization logs reported `HeadObject` 403. The uploaded IAM template already contains `s3:GetObject` for `doctors/*`; confirm the deployed role policy, actual S3 key, bucket policy and KMS configuration before changing permissions. A missing object may also appear as 403 without `s3:ListBucket`.
- The existing dashboard places the API access token in its Flask signed session and renders it into JavaScript. For production, migrate to server-side session storage and a backend-for-frontend proxy, enforce HTTPS and secure cookies, add CSRF protection to Flask forms, and audit CORS.
- The existing `/slots` endpoint has a scan fallback; the new doctor-scoped slot endpoint uses the `gsi-doctor-time` index.
- No appointment booking UI is included in this patient MVP; it provides the requested slot browser.
