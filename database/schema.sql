CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(60) NOT NULL,
    role ENUM('patient', 'doctor') NOT NULL,
    status ENUM('active', 'inactive', 'suspended') NOT NULL DEFAULT 'active',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email),
    KEY idx_users_role_status (role, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    PRIMARY KEY (specialisation_id),
    UNIQUE KEY uq_specialisations_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(200) NOT NULL,
    address VARCHAR(300) NULL,
    city VARCHAR(100) NOT NULL,
    status ENUM('active', 'inactive') NOT NULL DEFAULT 'active',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (hospital_id),
    UNIQUE KEY uq_hospitals_name_city (name, city),
    KEY idx_hospitals_city (city),
    KEY idx_hospitals_status_city (status, city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS doctors (
    user_id BIGINT UNSIGNED NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    license_number VARCHAR(50) NOT NULL,
    specialisation_id INT UNSIGNED NOT NULL,
    hospital_id INT UNSIGNED NULL,
    years_experience TINYINT UNSIGNED NULL,
    qualifications VARCHAR(500) NULL,
    languages VARCHAR(200) NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    photo_key VARCHAR(300) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_doctors_license (license_number),
    KEY idx_doctors_specialisation_hospital (specialisation_id, hospital_id),
    KEY idx_doctors_full_name (full_name),
    CONSTRAINT fk_doctors_user FOREIGN KEY (user_id) REFERENCES users(user_id),
    CONSTRAINT fk_doctors_specialisation FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id),
    CONSTRAINT fk_doctors_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS patients (
    user_id BIGINT UNSIGNED NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    date_of_birth DATE NULL,
    gender VARCHAR(20) NULL,
    phone VARCHAR(30) NULL,
    city VARCHAR(100) NULL,
    emergency_contact_name VARCHAR(150) NULL,
    emergency_contact_phone VARCHAR(30) NULL,
    notes TEXT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    KEY idx_patients_city (city),
    CONSTRAINT fk_patients_user FOREIGN KEY (user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id BIGINT UNSIGNED NOT NULL,
    name VARCHAR(40) NOT NULL,
    duration_min SMALLINT UNSIGNED NOT NULL,
    fee_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_type_doctor_name (doctor_user_id, name),
    KEY idx_consultation_types_doctor_active (doctor_user_id, is_active),
    CONSTRAINT fk_consultation_types_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT chk_consultation_duration CHECK (duration_min BETWEEN 5 AND 180),
    CONSTRAINT chk_consultation_fee CHECK (fee_amount >= 0),
    CONSTRAINT chk_consultation_capacity CHECK (capacity BETWEEN 1 AND 50)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS availability_patterns (
    pattern_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (pattern_id),
    KEY idx_availability_doctor_day (doctor_user_id, day_of_week, start_time),
    KEY idx_availability_consultation (consultation_type_id),
    CONSTRAINT fk_availability_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_availability_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_availability_day CHECK (day_of_week BETWEEN 1 AND 7),
    CONSTRAINT chk_availability_time CHECK (start_time < end_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id BIGINT UNSIGNED NOT NULL,
    blackout_date DATE NOT NULL,
    reason VARCHAR(200) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (blackout_id),
    UNIQUE KEY uq_blackout_doctor_date (doctor_user_id, blackout_date),
    CONSTRAINT fk_blackout_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS availability_exceptions (
    exception_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    exception_date DATE NOT NULL,
    exception_type VARCHAR(30) NOT NULL,
    start_time TIME NULL,
    end_time TIME NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (exception_id),
    KEY idx_availability_exceptions_doctor_date (doctor_user_id, exception_date),
    CONSTRAINT fk_availability_exceptions_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_availability_exceptions_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_availability_exception_time CHECK (start_time IS NULL OR end_time IS NULL OR start_time < end_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    patient_user_id BIGINT UNSIGNED NOT NULL,
    doctor_user_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    slot_id VARCHAR(100) NOT NULL,
    slot_start_utc DATETIME(3) NOT NULL,
    slot_end_utc DATETIME(3) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'booked',
    active_flag TINYINT NULL DEFAULT 1,
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    fee_amount DECIMAL(10,2) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_appointments_patient_slot_active (patient_user_id, slot_id, active_flag),
    KEY idx_appointments_patient_start (patient_user_id, slot_start_utc),
    KEY idx_appointments_doctor_start (doctor_user_id, slot_start_utc),
    KEY idx_appointments_patient_doctor_status (patient_user_id, doctor_user_id, status),
    KEY idx_appointments_status_end (status, slot_end_utc),
    CONSTRAINT fk_appointments_patient FOREIGN KEY (patient_user_id) REFERENCES patients(user_id),
    CONSTRAINT fk_appointments_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_appointments_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_appointment_time CHECK (slot_start_utc < slot_end_utc),
    CONSTRAINT chk_appointment_fee CHECK (fee_amount >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    patient_user_id BIGINT UNSIGNED NOT NULL,
    reason ENUM('PATIENT_CANCEL', 'DOCTOR_CANCEL', 'NO_SHOW') NOT NULL,
    tier VARCHAR(30) NULL,
    refund_percent TINYINT UNSIGNED NOT NULL DEFAULT 0,
    fee_amount DECIMAL(10,2) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_appointment (appointment_id),
    KEY idx_refund_patient_created (patient_user_id, created_at),
    CONSTRAINT fk_refund_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    CONSTRAINT fk_refund_patient FOREIGN KEY (patient_user_id) REFERENCES patients(user_id),
    CONSTRAINT chk_refund_percent CHECK (refund_percent BETWEEN 0 AND 100),
    CONSTRAINT chk_refund_amounts CHECK (fee_amount >= 0 AND refund_amount >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- REFERENCE DATA: SPECIALISATIONS
-- =====================================================

INSERT INTO specialisations (name)
VALUES
    ('General Medicine'),
    ('Cardiology'),
    ('Dermatology'),
    ('Neurology'),
    ('Orthopaedics'),
    ('Paediatrics'),
    ('Gynaecology'),
    ('Ophthalmology'),
    ('ENT'),
    ('Psychiatry')
ON DUPLICATE KEY UPDATE
    name = VALUES(name);

-- =====================================================
-- REFERENCE DATA: HOSPITALS
-- =====================================================

INSERT INTO hospitals (name, address, city, status)
VALUES
    (
        'MediSlot Demo Hospital',
        'Hyderabad, Telangana',
        'Hyderabad',
        'active'
    )
ON DUPLICATE KEY UPDATE
    address = VALUES(address),
    status = VALUES(status);