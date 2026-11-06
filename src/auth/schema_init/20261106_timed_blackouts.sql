-- Run ONCE on the existing MediSlot MySQL database before deploying the API.
-- Take a database snapshot first.
ALTER TABLE blackout_dates
  ADD COLUMN is_full_day TINYINT(1) NOT NULL DEFAULT 1 AFTER reason,
  ADD COLUMN start_time TIME NULL AFTER is_full_day,
  ADD COLUMN end_time TIME NULL AFTER start_time;

ALTER TABLE blackout_dates
  DROP INDEX uq_blackout_doctor_date,
  ADD INDEX idx_blackout_doctor_date (doctor_user_id, blackout_date);
