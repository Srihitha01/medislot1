

-- Patient in-app notifications (SNS email delivery is a separate channel).
CREATE TABLE IF NOT EXISTS patient_notifications (
    notification_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    patient_user_id BIGINT UNSIGNED NOT NULL,
    appointment_id BIGINT UNSIGNED NULL,
    notification_type VARCHAR(50) NOT NULL,
    title VARCHAR(180) NOT NULL,
    message TEXT NOT NULL,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (notification_id),
    KEY idx_patient_notifications_user_created (patient_user_id, created_at),
    CONSTRAINT fk_patient_notifications_user FOREIGN KEY (patient_user_id) REFERENCES patients(user_id),
    CONSTRAINT fk_patient_notifications_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
