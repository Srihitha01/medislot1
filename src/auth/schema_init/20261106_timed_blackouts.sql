-- Historical manual migration. The schema initializer handles this automatically.
-- This SQL is for an UNMIGRATED database only; do not rerun after auto-migration.
-- Take an RDS snapshot before any DDL.
ALTER TABLE blackout_dates
  ADD COLUMN is_full_day TINYINT(1) NOT NULL DEFAULT 1 AFTER reason,
  ADD COLUMN start_time TIME NULL AFTER is_full_day,
  ADD COLUMN end_time TIME NULL AFTER start_time;

-- The replacement FK-supporting index MUST exist before dropping the old one.
ALTER TABLE blackout_dates
  ADD INDEX idx_blackout_doctor_date (doctor_user_id, blackout_date);

ALTER TABLE blackout_dates
  DROP INDEX uq_blackout_doctor_date;
