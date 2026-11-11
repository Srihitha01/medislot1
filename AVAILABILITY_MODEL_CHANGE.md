# MediSlot availability model change

## New model

MediSlot now uses only two doctor scheduling tables:

- `availability_patterns`: recurring weekly working hours. Doctors can create, edit, and delete these patterns.
- `blackout_dates`: date-specific unavailability. Supports full-day and timed blackouts.

The date-specific `availability_exceptions` model and API route have been removed. The slot materialiser now creates slots from weekly patterns and applies blackout dates.

## Existing data and migration

The GitHub Actions deployment workflow already invokes the schema initializer with `{"action":"initialize"}`. The initializer runs an idempotent cleanup:

1. Ensures the current `blackout_dates` columns exist.
2. Copies legacy `CLOSED` exception dates into `blackout_dates` as full-day blackouts, avoiding duplicates.
3. Drops the legacy `availability_exceptions` table.

Legacy `ADD` and `OVERRIDE` exception rows are not representable in the simplified model and are discarded when the table is dropped. Before deployment, if you want to check whether any exist, run `SELECT exception_type, COUNT(*) FROM availability_exceptions GROUP BY exception_type;` in MySQL Workbench. Review any `ADD` or `OVERRIDE` rows if their date-specific changes must be retained.

A manual SQL version is provided at:
`database/migrations/20261111_remove_availability_exceptions.sql`

Doctor, patient, appointment, consultation-type, availability-pattern and blackout records are otherwise preserved. No UUIDs are introduced; existing numeric IDs remain unchanged.

## Doctor timing changes

Doctors continue to update recurring timings through:

- `GET /doctors/me/availability-patterns`
- `POST /doctors/me/availability-patterns`
- `PUT/PATCH /doctors/me/availability-patterns/{patternId}`
- `DELETE /doctors/me/availability-patterns/{patternId}`

The dashboard's weekly availability editor continues to use the existing pattern ID internally, while displaying the consultation type name rather than an ID.

## Deployment

Deploy the updated Lambda packages and dashboard through the existing CloudFormation/GitHub Actions workflow. The schema initializer's `initialize` action performs the table cleanup; no full database reset is required.
