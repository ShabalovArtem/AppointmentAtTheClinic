-- Скидання дня dd-joint спільного досліду (записи обох каналів і запити кол-центру).
BEGIN;
DELETE FROM booking WHERE doctor_day_id = 'dd-joint';
DELETE FROM booking_request WHERE doctor_day_id = 'dd-joint';
UPDATE doctor_day SET slots_taken = 0 WHERE id = 'dd-joint';
COMMIT;
