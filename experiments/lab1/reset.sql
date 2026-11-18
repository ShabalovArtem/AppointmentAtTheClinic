-- Скидання тестових ресурсів досліду О1.4 (race-locked, race-unprotected) до початкового стану.
BEGIN;
DELETE FROM booking WHERE doctor_day_id IN ('race-locked', 'race-unprotected');
DELETE FROM booking_request WHERE doctor_day_id IN ('race-locked', 'race-unprotected');
UPDATE doctor_day SET slots_taken = 0 WHERE id IN ('race-locked', 'race-unprotected');
COMMIT;
