-- Скидання стану сайту до демоданих: записи сайту, ключі ідемпотентності, сесії, зареєстровані під час
-- перевірок пацієнти, закриті дні; перерахунок зайнятих слотів за активними записами (записи кол-центру лишаються).
BEGIN;
DELETE FROM booking WHERE channel = 'web';
DELETE FROM idempotency_key;
DELETE FROM user_session;
DELETE FROM app_user WHERE id NOT IN ('p-1001', 'p-1002', 'p-1003', 'p-1004', 'p-1005', 'u-registrar');
UPDATE doctor_day d SET closed = false, slots_taken = coalesce((
  SELECT sum(b.slots) FROM booking b
  WHERE b.doctor_day_id = d.id AND b.status IN ('held', 'confirmed', 'visited', 'no_show')), 0)
WHERE NOT d.unprotected_test;
-- тестовий день режиму без захисту (лише записи сайту)
UPDATE doctor_day SET slots_taken = 0 WHERE id = 'web-race-unprotected';
COMMIT;
