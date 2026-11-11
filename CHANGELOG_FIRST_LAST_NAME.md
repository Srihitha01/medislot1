# Change log — first and last name registration

- Doctor and patient registration collect `first_name` and `last_name` separately.
- The registration Lambda validates both fields and stores them in separate `users` columns.
- The existing `users.name` field is still populated with `First Last` for compatibility.
- Existing `doctors.full_name` and `patients.full_name` fields continue to be populated for existing dashboard/search code.
- Legacy clients sending a single `name` are still accepted; a single-token legacy name is stored with an empty `last_name`.

## Database migration

For an existing database, run `src/auth/schema_init/20261110_user_first_last_name.sql` once before deploying the updated Lambda. Back up the database first. The migration adds the two columns and backfills existing rows from `users.name`.

Both fresh-install schema files (`src/auth/schema_init/schema.sql` and `database/schema.sql`) include the new columns.
