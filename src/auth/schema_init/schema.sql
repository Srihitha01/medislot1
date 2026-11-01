CREATE TABLE IF NOT EXISTS users (
    user_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(60) NOT NULL,
    role ENUM('patient','doctor') NOT NULL,
    status ENUM('active','suspended') NOT NULL DEFAULT 'active',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    PRIMARY KEY (specialisation_id),
    UNIQUE KEY uq_specialisations_name (name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(200) NOT NULL,
    address VARCHAR(300) NOT NULL,
    city VARCHAR(100) NOT NULL,
    status ENUM('active','inactive') NOT NULL DEFAULT 'active',
    PRIMARY KEY (hospital_id),
    UNIQUE KEY uq_hospitals_name_city (name, city)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS patients (
    user_id INT UNSIGNED NOT NULL,
    date_of_birth DATE NOT NULL,
    gender VARCHAR(20) NOT NULL,
    phone VARCHAR(30) NOT NULL,
    city VARCHAR(100) NOT NULL,
    emergency_contact_name VARCHAR(150) NULL,
    emergency_contact_phone VARCHAR(30) NULL,
    notes TEXT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    CONSTRAINT fk_patients_user FOREIGN KEY (user_id) REFERENCES users(user_id),
    CONSTRAINT chk_patients_gender CHECK (gender IN ('female','male','non_binary','prefer_not_to_say'))
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctors (
    user_id INT UNSIGNED NOT NULL,
    license_number VARCHAR(50) NOT NULL,
    specialisation_id INT UNSIGNED NOT NULL,
    hospital_id INT UNSIGNED NULL,
    years_experience TINYINT UNSIGNED NOT NULL DEFAULT 0,
    qualifications VARCHAR(500) NOT NULL,
    languages VARCHAR(200) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    photo_key VARCHAR(300) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_doctors_license (license_number),
    CONSTRAINT fk_doctors_user FOREIGN KEY (user_id) REFERENCES users(user_id),
    CONSTRAINT fk_doctors_specialisation FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id),
    CONSTRAINT fk_doctors_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id INT UNSIGNED NOT NULL,
    name ENUM('video','in_clinic','phone') NOT NULL,
    duration_min SMALLINT UNSIGNED NOT NULL,
    fee_amount DECIMAL(10,2) NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_doctor_name (doctor_user_id, name),
    CONSTRAINT fk_consultation_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT chk_consultation_duration CHECK (duration_min BETWEEN 5 AND 180),
    CONSTRAINT chk_consultation_capacity CHECK (capacity BETWEEN 1 AND 50),
    CONSTRAINT chk_consultation_fee CHECK (fee_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS availability_patterns (
    pattern_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id INT UNSIGNED NOT NULL,
    consultation_type_id INT UNSIGNED NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL COMMENT '1=Monday ... 7=Sunday',
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    PRIMARY KEY (pattern_id),
    UNIQUE KEY uq_pattern_exact (doctor_user_id, consultation_type_id, day_of_week, start_time, end_time),
    KEY ix_pattern_doctor_day (doctor_user_id, day_of_week),
    CONSTRAINT fk_pattern_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_pattern_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_pattern_day CHECK (day_of_week BETWEEN 1 AND 7),
    CONSTRAINT chk_pattern_time CHECK (end_time > start_time)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id INT UNSIGNED NOT NULL,
    blackout_date DATE NOT NULL,
    reason VARCHAR(200) NULL,
    PRIMARY KEY (blackout_id),
    UNIQUE KEY uq_blackout_doctor_date (doctor_user_id, blackout_date),
    CONSTRAINT fk_blackout_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS availability_exceptions (
    exception_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_user_id INT UNSIGNED NOT NULL,
    consultation_type_id INT UNSIGNED NOT NULL,
    exception_date DATE NOT NULL,
    exception_type ENUM('ADD','OVERRIDE','CLOSED') NOT NULL,
    start_time TIME NULL,
    end_time TIME NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (exception_id),
    KEY ix_exception_doctor_date (doctor_user_id, exception_date),
    CONSTRAINT fk_exception_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_exception_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id),
    CONSTRAINT chk_exception_window CHECK (
      (exception_type='CLOSED' AND start_time IS NULL AND end_time IS NULL)
      OR (exception_type IN ('ADD','OVERRIDE') AND start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time)
    )
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    patient_user_id INT UNSIGNED NOT NULL,
    doctor_user_id INT UNSIGNED NOT NULL,
    consultation_type_id INT UNSIGNED NOT NULL,
    slot_id VARCHAR(255) NOT NULL,
    slot_start_utc DATETIME(3) NOT NULL,
    slot_end_utc DATETIME(3) NOT NULL,
    status ENUM('booked','cancelled','completed','no_show') NOT NULL DEFAULT 'booked',
    active_flag TINYINT(1) NOT NULL DEFAULT 1,
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    fee_amount DECIMAL(10,2) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_appointment_patient_slot (patient_user_id, slot_id),
    KEY ix_appointments_patient (patient_user_id, slot_start_utc),
    KEY ix_appointments_doctor (doctor_user_id, slot_start_utc),
    CONSTRAINT fk_appointments_patient FOREIGN KEY (patient_user_id) REFERENCES patients(user_id),
    CONSTRAINT fk_appointments_doctor FOREIGN KEY (doctor_user_id) REFERENCES doctors(user_id),
    CONSTRAINT fk_appointments_consultation FOREIGN KEY (consultation_type_id) REFERENCES consultation_types(consultation_type_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    patient_user_id INT UNSIGNED NOT NULL,
    reason VARCHAR(100) NOT NULL,
    tier VARCHAR(30) NOT NULL,
    refund_percent TINYINT UNSIGNED NOT NULL,
    fee_amount DECIMAL(10,2) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_appointment (appointment_id),
    CONSTRAINT fk_refund_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    CONSTRAINT fk_refund_patient FOREIGN KEY (patient_user_id) REFERENCES patients(user_id)
) ENGINE=InnoDB;


INSERT IGNORE INTO specialisations (name) VALUES
('Cardiology'),('Dermatology'),('Neurology'),('Orthopedics'),
('Pediatrics'),('General Medicine'),('Gynecology'),('Ophthalmology');
