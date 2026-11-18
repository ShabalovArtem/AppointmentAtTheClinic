-- C02 «Запис до клініки», ЛР1.
-- Запит на запис (кол-центр) приймається в обмежену чергу; обробник перевіряє поліс у страховій
-- (повільний виклик) і займає слоти дня лікаря.
-- Інваріант ресурсу doctor_day: slots_taken = сума слотів у booking і slots_taken <= slots_total.
-- CHECK (slots_taken <= slots_total) навмисно немає: дослід О1.4 без синхронізації
-- має змогу показати порушення інваріанта.

CREATE TABLE doctor (
  id         text PRIMARY KEY,
  full_name  text NOT NULL,
  specialty  text NOT NULL
);

CREATE TABLE doctor_day (
  id           text PRIMARY KEY,
  doctor_id    text NOT NULL REFERENCES doctor (id),
  day          date NOT NULL,
  slots_total  integer NOT NULL CHECK (slots_total > 0),
  slots_taken  integer NOT NULL DEFAULT 0
);

-- Предметний ефект: зайняття слотів; ключ — request_id (один запит займає слоти не більше одного разу).
CREATE TABLE booking (
  request_id     text PRIMARY KEY,
  doctor_day_id  text NOT NULL REFERENCES doctor_day (id),
  patient_id     text NOT NULL,
  slots          integer NOT NULL CHECK (slots > 0),
  booked_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX booking_doctor_day ON booking (doctor_day_id);

-- Запит на запис і його стан: queued -> processing -> done | rejected | failed.
CREATE TABLE booking_request (
  id             text PRIMARY KEY,
  doctor_day_id  text NOT NULL REFERENCES doctor_day (id),
  patient_id     text NOT NULL,
  slots          integer NOT NULL CHECK (slots > 0),
  state          text NOT NULL CHECK (state IN ('queued', 'processing', 'done', 'rejected', 'failed')),
  reason         text,
  received_at    timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  finished_at    timestamptz,
  first_read_at  timestamptz
);

CREATE INDEX booking_request_state ON booking_request (state);
