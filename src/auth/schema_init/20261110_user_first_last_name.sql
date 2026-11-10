-- MediSlot migration: persist first and last names separately.
-- Run once on an existing MySQL database before deploying the updated register Lambda.
-- The legacy `name` column is retained so existing queries and integrations continue to work.

ALTER TABLE users
    ADD COLUMN first_name VARCHAR(75) NULL AFTER name,
    ADD COLUMN last_name  VARCHAR(75) NULL AFTER first_name;

-- Backfill existing accounts. The first token becomes first_name; the remainder
-- (including any middle names) is retained in last_name.
UPDATE users
SET
    first_name = CASE
        WHEN LOCATE(' ', TRIM(name)) = 0 THEN TRIM(name)
        ELSE SUBSTRING_INDEX(TRIM(name), ' ', 1)
    END,
    last_name = CASE
        WHEN LOCATE(' ', TRIM(name)) = 0 THEN ''
        ELSE TRIM(SUBSTRING(TRIM(name), LOCATE(' ', TRIM(name)) + 1))
    END
WHERE first_name IS NULL OR last_name IS NULL;

-- New registrations require both fields; allow empty last_name for legacy
-- single-word names, while the application validates new signups.
ALTER TABLE users
    MODIFY COLUMN first_name VARCHAR(75) NOT NULL,
    MODIFY COLUMN last_name VARCHAR(75) NOT NULL;
