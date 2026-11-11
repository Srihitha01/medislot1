-- MediSlot: remove the obsolete date-specific availability_exceptions table.
-- The schema initializer performs this migration automatically on initialize.
-- If running manually, execute this script once against the medislot database.
-- CLOSED rows are copied into blackout_dates; ADD/OVERRIDE rows are not supported
-- in the simplified model and will be discarded when the old table is dropped.

INSERT INTO blackout_dates (
    doctor_user_id, blackout_date, reason, is_full_day, start_time, end_time
)
SELECT
    ae.doctor_user_id,
    ae.exception_date,
    'Migrated from a closed availability exception',
    1,
    NULL,
    NULL
FROM availability_exceptions ae
LEFT JOIN blackout_dates bd
  ON bd.doctor_user_id = ae.doctor_user_id
 AND bd.blackout_date = ae.exception_date
 AND bd.is_full_day = 1
WHERE UPPER(ae.exception_type) = 'CLOSED'
  AND bd.blackout_id IS NULL;

DROP TABLE IF EXISTS availability_exceptions;
