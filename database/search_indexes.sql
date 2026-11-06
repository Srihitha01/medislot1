-- Optional additional indexes. Apply only after EXPLAIN confirms benefit.
-- Existing schema already has idx_doctors_full_name, idx_doctors_specialisation_hospital,
-- idx_hospitals_city and idx_hospitals_status_city. Avoid duplicating those.
CREATE INDEX idx_hospitals_name_search ON hospitals (name, hospital_id);
CREATE INDEX idx_doctors_specialisation_name ON doctors (specialisation_id, full_name, user_id);
-- Prefix searches use LIKE 'text%'; index use for joined filters depends on optimizer.
