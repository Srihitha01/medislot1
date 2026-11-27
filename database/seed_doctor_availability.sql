-- MediSlot dummy data: doctors with different kinds of availability.
-- Safe to re-run: doctors/types are upserted; their patterns and blackouts are rebuilt.
-- Login for every dummy doctor: <email below> with the shared dummy password you chose.
-- Run once against the medislot database AFTER schema.sql (needs the specialisations
-- and the 'MediSlot Demo Hospital' row it seeds). Run the file itself (mysql < file) rather
-- than pasting it through a shell, because the password hash contains '$' characters.

SET @next_monday = DATE_ADD(CURDATE(), INTERVAL (7 - WEEKDAY(CURDATE())) DAY);

DROP TEMPORARY TABLE IF EXISTS tmp_doc;
DROP TEMPORARY TABLE IF EXISTS tmp_ct;
DROP TEMPORARY TABLE IF EXISTS tmp_pat;
DROP TEMPORARY TABLE IF EXISTS tmp_bl;
CREATE TEMPORARY TABLE tmp_doc (email VARCHAR(254), first_name VARCHAR(75), last_name VARCHAR(75),
  spec VARCHAR(100), license VARCHAR(50), years TINYINT UNSIGNED, quals VARCHAR(500), langs VARCHAR(200))
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TEMPORARY TABLE tmp_ct (email VARCHAR(254), name VARCHAR(40), duration SMALLINT UNSIGNED,
  fee DECIMAL(10,2), capacity TINYINT UNSIGNED, active TINYINT(1))
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TEMPORARY TABLE tmp_pat (email VARCHAR(254), ctype VARCHAR(40), dow TINYINT UNSIGNED, st TIME, et TIME)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TEMPORARY TABLE tmp_bl (email VARCHAR(254), bdate DATE, reason VARCHAR(200),
  full_day TINYINT(1), st TIME NULL, et TIME NULL)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO tmp_doc VALUES
  ('ananya.reddy@medislot.demo','Ananya','Reddy','General Medicine','TSMC-D1001',12,'MBBS, MD (General Medicine)','English, Telugu, Hindi'),
  ('rahul.verma@medislot.demo','Rahul','Verma','Cardiology','TSMC-D1002',18,'MBBS, MD, DM (Cardiology)','English, Hindi'),
  ('meera.iyer@medislot.demo','Meera','Iyer','Dermatology','TSMC-D1003',9,'MBBS, MD (Dermatology)','English, Tamil, Telugu'),
  ('kiran.patel@medislot.demo','Kiran','Patel','Paediatrics','TSMC-D1004',14,'MBBS, DCH, MD (Paediatrics)','English, Hindi, Gujarati'),
  ('sana.khan@medislot.demo','Sana','Khan','Gynaecology','TSMC-D1005',11,'MBBS, MS (Obstetrics & Gynaecology)','English, Urdu, Telugu'),
  ('arjun.nair@medislot.demo','Arjun','Nair','Neurology','TSMC-D1006',16,'MBBS, MD, DM (Neurology)','English, Malayalam, Hindi'),
  ('lakshmi.rao@medislot.demo','Lakshmi','Rao','ENT','TSMC-D1007',8,'MBBS, MS (ENT)','English, Telugu'),
  ('vikram.singh@medislot.demo','Vikram','Singh','Psychiatry','TSMC-D1008',13,'MBBS, MD (Psychiatry)','English, Hindi, Punjabi'),
  ('imran.ali@medislot.demo','Imran','Ali','Orthopaedics','TSMC-D1009',20,'MBBS, MS (Orthopaedics)','English, Urdu, Telugu'),
  ('pooja.sharma@medislot.demo','Pooja','Sharma','Ophthalmology','TSMC-D1010',3,'MBBS, DNB (Ophthalmology)','English, Hindi');

INSERT INTO tmp_ct VALUES
  ('ananya.reddy@medislot.demo','General Consultation',30,500,1,1),
  ('rahul.verma@medislot.demo','Cardiology Consultation',30,900,1,1),
  ('meera.iyer@medislot.demo','Skin Consultation',20,700,1,1),
  ('kiran.patel@medislot.demo','New Patient Visit',30,600,1,1),
  ('kiran.patel@medislot.demo','Follow-up Visit',15,300,1,1),
  ('kiran.patel@medislot.demo','Video Consultation',20,400,1,1),
  ('sana.khan@medislot.demo','Health Camp Session',60,200,8,1),
  ('sana.khan@medislot.demo','Gynae Consultation',30,800,1,1),
  ('arjun.nair@medislot.demo','Evening Consultation',30,1000,1,1),
  ('lakshmi.rao@medislot.demo','ENT Consultation',15,450,1,1),
  ('vikram.singh@medislot.demo','Counselling Session',45,1200,1,1),
  ('vikram.singh@medislot.demo','Legacy Phone Consult',30,600,1,0),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',15,700,1,1),
  ('pooja.sharma@medislot.demo','Eye Check-up',20,550,1,1);

INSERT INTO tmp_pat VALUES
  ('ananya.reddy@medislot.demo','General Consultation',1,'09:00:00','13:00:00'),
  ('ananya.reddy@medislot.demo','General Consultation',2,'09:00:00','13:00:00'),
  ('ananya.reddy@medislot.demo','General Consultation',3,'09:00:00','13:00:00'),
  ('ananya.reddy@medislot.demo','General Consultation',4,'09:00:00','13:00:00'),
  ('ananya.reddy@medislot.demo','General Consultation',5,'09:00:00','13:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',1,'09:00:00','12:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',3,'09:00:00','12:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',5,'09:00:00','12:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',1,'17:00:00','20:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',3,'17:00:00','20:00:00'),
  ('rahul.verma@medislot.demo','Cardiology Consultation',5,'17:00:00','20:00:00'),
  ('meera.iyer@medislot.demo','Skin Consultation',6,'10:00:00','16:00:00'),
  ('meera.iyer@medislot.demo','Skin Consultation',7,'10:00:00','14:00:00'),
  ('kiran.patel@medislot.demo','New Patient Visit',1,'10:00:00','12:00:00'),
  ('kiran.patel@medislot.demo','New Patient Visit',2,'10:00:00','12:00:00'),
  ('kiran.patel@medislot.demo','New Patient Visit',3,'10:00:00','12:00:00'),
  ('kiran.patel@medislot.demo','New Patient Visit',4,'10:00:00','12:00:00'),
  ('kiran.patel@medislot.demo','Follow-up Visit',1,'12:00:00','13:00:00'),
  ('kiran.patel@medislot.demo','Follow-up Visit',2,'12:00:00','13:00:00'),
  ('kiran.patel@medislot.demo','Follow-up Visit',3,'12:00:00','13:00:00'),
  ('kiran.patel@medislot.demo','Follow-up Visit',4,'12:00:00','13:00:00'),
  ('kiran.patel@medislot.demo','Video Consultation',1,'18:00:00','20:00:00'),
  ('kiran.patel@medislot.demo','Video Consultation',3,'18:00:00','20:00:00'),
  ('sana.khan@medislot.demo','Health Camp Session',6,'09:00:00','12:00:00'),
  ('sana.khan@medislot.demo','Gynae Consultation',2,'14:00:00','17:00:00'),
  ('sana.khan@medislot.demo','Gynae Consultation',4,'14:00:00','17:00:00'),
  ('arjun.nair@medislot.demo','Evening Consultation',2,'18:00:00','23:00:00'),
  ('arjun.nair@medislot.demo','Evening Consultation',3,'18:00:00','23:00:00'),
  ('arjun.nair@medislot.demo','Evening Consultation',4,'18:00:00','23:00:00'),
  ('arjun.nair@medislot.demo','Evening Consultation',5,'18:00:00','23:00:00'),
  ('arjun.nair@medislot.demo','Evening Consultation',6,'18:00:00','23:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',1,'10:00:00','14:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',2,'10:00:00','14:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',3,'10:00:00','14:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',4,'10:00:00','14:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',5,'10:00:00','14:00:00'),
  ('lakshmi.rao@medislot.demo','ENT Consultation',6,'10:00:00','14:00:00'),
  ('vikram.singh@medislot.demo','Counselling Session',1,'13:30:00','18:00:00'),
  ('vikram.singh@medislot.demo','Counselling Session',2,'13:30:00','18:00:00'),
  ('vikram.singh@medislot.demo','Counselling Session',3,'13:30:00','18:00:00'),
  ('vikram.singh@medislot.demo','Counselling Session',4,'13:30:00','18:00:00'),
  ('vikram.singh@medislot.demo','Counselling Session',5,'13:30:00','18:00:00'),
  ('vikram.singh@medislot.demo','Legacy Phone Consult',1,'08:00:00','09:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',1,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',2,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',3,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',4,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',5,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',6,'08:00:00','13:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',1,'14:00:00','18:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',2,'14:00:00','18:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',3,'14:00:00','18:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',4,'14:00:00','18:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',5,'14:00:00','18:00:00'),
  ('imran.ali@medislot.demo','Orthopaedic Consultation',6,'14:00:00','18:00:00');

INSERT INTO tmp_bl VALUES
  ('ananya.reddy@medislot.demo',DATE_ADD(@next_monday, INTERVAL 3 DAY),'Public holiday',1,NULL,NULL),
  ('rahul.verma@medislot.demo',DATE_ADD(@next_monday, INTERVAL 2 DAY),'Cath lab procedure',0,'17:00:00','18:30:00'),
  ('lakshmi.rao@medislot.demo',DATE_ADD(@next_monday, INTERVAL 0 DAY),'Annual leave',1,NULL,NULL),
  ('lakshmi.rao@medislot.demo',DATE_ADD(@next_monday, INTERVAL 2 DAY),'Medical conference',1,NULL,NULL),
  ('lakshmi.rao@medislot.demo',DATE_ADD(@next_monday, INTERVAL 4 DAY),'Minor procedure',0,'11:00:00','12:30:00'),
  ('vikram.singh@medislot.demo',DATE_ADD(@next_monday, INTERVAL 1 DAY),'Training workshop',1,NULL,NULL);

-- 1. Login accounts
INSERT INTO users (name, first_name, last_name, email, password_hash, role, status)
SELECT CONCAT('Dr. ', first_name, ' ', last_name), first_name, last_name, email,
       '$2b$12$p6f3B1ax9YSLdFd21/fC4O/tdrjas4N2TKDYZMgSG/moi2gyvtx2e', 'doctor', 'active'
FROM tmp_doc
ON DUPLICATE KEY UPDATE name = VALUES(name), password_hash = VALUES(password_hash), status = 'active';

-- 2. Doctor profiles
INSERT INTO doctors (user_id, full_name, license_number, specialisation_id, hospital_id,
                     years_experience, qualifications, languages, timezone)
SELECT u.user_id, CONCAT('Dr. ', t.first_name, ' ', t.last_name), t.license, s.specialisation_id,
       h.hospital_id, t.years, t.quals, t.langs, 'Asia/Kolkata'
FROM tmp_doc t
JOIN users u ON u.email = t.email
JOIN specialisations s ON s.name = t.spec
JOIN hospitals h ON h.name = 'MediSlot Demo Hospital' AND h.city = 'Hyderabad'
ON DUPLICATE KEY UPDATE full_name = VALUES(full_name), specialisation_id = VALUES(specialisation_id),
  hospital_id = VALUES(hospital_id), years_experience = VALUES(years_experience),
  qualifications = VALUES(qualifications), languages = VALUES(languages);

-- 3. Consultation types
INSERT INTO consultation_types (doctor_user_id, name, duration_min, fee_amount, capacity, is_active)
SELECT u.user_id, t.name, t.duration, t.fee, t.capacity, t.active
FROM tmp_ct t JOIN users u ON u.email = t.email
ON DUPLICATE KEY UPDATE duration_min = VALUES(duration_min), fee_amount = VALUES(fee_amount),
  capacity = VALUES(capacity), is_active = VALUES(is_active);

-- 4. Weekly availability (rebuilt on every run). day_of_week: Monday = 1 ... Sunday = 7
DELETE FROM availability_patterns
WHERE doctor_user_id IN (SELECT user_id FROM users WHERE email LIKE '%@medislot.demo' AND role = 'doctor');

INSERT INTO availability_patterns (doctor_user_id, consultation_type_id, day_of_week, start_time, end_time)
SELECT u.user_id, ct.consultation_type_id, t.dow, t.st, t.et
FROM tmp_pat t
JOIN users u ON u.email = t.email
JOIN consultation_types ct ON ct.doctor_user_id = u.user_id AND ct.name = t.ctype;

-- 5. Leave / holidays: full-day and part-day blackouts, always in the coming 1-2 weeks
DELETE FROM blackout_dates
WHERE doctor_user_id IN (SELECT user_id FROM users WHERE email LIKE '%@medislot.demo' AND role = 'doctor');

INSERT INTO blackout_dates (doctor_user_id, blackout_date, reason, is_full_day, start_time, end_time)
SELECT u.user_id, t.bdate, t.reason, t.full_day, t.st, t.et
FROM tmp_bl t JOIN users u ON u.email = t.email;

DROP TEMPORARY TABLE tmp_doc;
DROP TEMPORARY TABLE tmp_ct;
DROP TEMPORARY TABLE tmp_pat;
DROP TEMPORARY TABLE tmp_bl;
