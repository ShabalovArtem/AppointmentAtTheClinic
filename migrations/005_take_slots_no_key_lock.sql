-- DPC ЛР3: усунення взаємоблокування в приймачі ефектів.
--
-- Приймач спершу вставляє рядок effect із зовнішнім ключем на doctor_day — PostgreSQL ставить на рядок дня
-- FOR KEY SHARE. Далі take_slots() брала FOR UPDATE, а воно конфліктує з KEY SHARE іншої такої самої транзакції:
-- дві транзакції чекали одна на одну (DeadlockDetected після deadlock_timeout = 1 с).
--
-- take_slots() змінює лише slots_taken (не ключ), тож достатньо FOR NO KEY UPDATE: воно так само серіалізує
-- «перевірку + зміну» між усіма викликачами (конфліктує саме з собою), але сумісне з FOR KEY SHARE вставок,
-- що посилаються на день. Правило й інваріант не змінюються.

CREATE OR REPLACE FUNCTION take_slots(p_doctor_day_id text, p_slots integer) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  d doctor_day%ROWTYPE;
BEGIN
  SELECT * INTO d FROM doctor_day WHERE id = p_doctor_day_id FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    RETURN 'doctor-day-not-found';
  END IF;
  IF d.closed THEN
    RETURN 'day-closed';
  END IF;
  IF d.slots_taken + p_slots > d.slots_total THEN
    RETURN 'no-free-slots';
  END IF;
  UPDATE doctor_day SET slots_taken = slots_taken + p_slots WHERE id = p_doctor_day_id;
  RETURN NULL;
END
$$;
