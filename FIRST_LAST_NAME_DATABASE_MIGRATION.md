# First and last name database update

The `users` table now stores `first_name` and `last_name` as separate columns.
The existing `name` column remains as a compatibility/display field so existing
queries and integrations are not broken. Doctor and patient profile tables keep
their existing `full_name` field, populated from the two submitted fields.

## Existing database

Before deploying the updated registration Lambda, run:

`src/auth/schema_init/20261110_user_first_last_name.sql`

Run it once against the same MySQL database used by MediSlot. Back up the database
first. The migration backfills existing records from `users.name`, then makes
both new columns non-null.

New registration requests should send `firstName` and `lastName` (or
`first_name` and `last_name`). The registration Lambda stores them separately
and also fills the legacy `users.name` and profile `full_name` values.
