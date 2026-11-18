-- C02 «Запис до клініки» — спільна схема частин Web і DPC (одна база PostgreSQL).
--
-- Хто що змінює (розділ 6 картки DPC):
--   * сайт записує пацієнтів через Web (POST /v1/bookings, бронювання в стані held);
--   * кол-центр записує через чергу DPC (POST /v1/booking-requests, бронювання confirmed);
--   * обидва канали займають слоти того самого doctor_day і застосовують ОДНЕ предметне правило —
--     функцію take_slots() нижче: перевірка й зміна ресурсу атомарно в базі, у транзакції каналу.
--
-- Інваріанти: 1) зайнятих слотів дня не більше, ніж є (slots_taken <= slots_total, і slots_taken
-- дорівнює сумі слотів активних бронювань); 2) у пацієнта не більше одного активного запису до
-- одного лікаря на день (частковий унікальний індекс).

CREATE TABLE app_user (
  id            text PRIMARY KEY,           -- для пацієнтів це й patient_id
  login         text NOT NULL UNIQUE,
  name          text NOT NULL,
  password_hash text NOT NULL,
  role          text NOT NULL CHECK (role IN ('patient', 'staff'))
);

CREATE TABLE doctor (
  id         text PRIMARY KEY,
  full_name  text NOT NULL,
  specialty  text NOT NULL
);

CREATE TABLE doctor_day (
  id               text PRIMARY KEY,
  doctor_id        text NOT NULL REFERENCES doctor (id),
  day              date NOT NULL,                                -- календарна дата прийому в зоні клініки
  clinic_tz        text NOT NULL DEFAULT 'Europe/Kyiv',          -- назва зони окремо від моментів
  slots_total      integer NOT NULL CHECK (slots_total > 0),     -- слоти по 15 хв
  slots_taken      integer NOT NULL DEFAULT 0,
  price_minor      bigint NOT NULL CHECK (price_minor >= 0),     -- вартість одного слоту, копійки
  currency         char(3) NOT NULL DEFAULT 'UAH' CHECK (currency ~ '^[A-Z]{3}$'),
  closed           boolean NOT NULL DEFAULT false,               -- реєстратор закрив день (ЛР3)
  -- Лише окремі тестові дні дослідів «без захисту» (DPC О1.4, Web О2.1) можуть порушити інваріант;
  -- для всіх інших його гарантує CHECK.
  unprotected_test boolean NOT NULL DEFAULT false,
  UNIQUE (doctor_id, day),
  CONSTRAINT doctor_day_slots_invariant CHECK (unprotected_test OR slots_taken BETWEEN 0 AND slots_total)
);

CREATE TABLE booking (
  id             text PRIMARY KEY,
  channel        text NOT NULL CHECK (channel IN ('web', 'call-center')),
  patient_id     text NOT NULL,     -- спільний ідентифікатор; пацієнт кол-центру може не мати облікового запису
  doctor_day_id  text NOT NULL REFERENCES doctor_day (id),
  slots          integer NOT NULL CHECK (slots BETWEEN 1 AND 2),
  visit_reason   text CHECK (char_length(visit_reason) <= 500),
  status         text NOT NULL CHECK (status IN ('held', 'confirmed', 'expired', 'cancelled', 'visited', 'no_show')),
  version        bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  request_id     text UNIQUE,       -- ключ ефекту DPC «зайняття слоту» (запит кол-центру)
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz,       -- строк утримання held
  CHECK (status <> 'held' OR expires_at IS NOT NULL),
  CHECK (channel <> 'call-center' OR request_id IS NOT NULL)
);

-- інваріант 2: один активний запис пацієнта на день лікаря (doctor_day = лікар + дата)
CREATE UNIQUE INDEX booking_one_active_per_patient_day
  ON booking (patient_id, doctor_day_id) WHERE status IN ('held', 'confirmed');
-- список «мої записи» від найновіших за парою (created_at, id)
CREATE INDEX booking_patient_newest ON booking (patient_id, created_at DESC, id DESC);
CREATE INDEX booking_doctor_day ON booking (doctor_day_id);

-- Запит кол-центру в черзі DPC: queued -> processing -> done | rejected | failed.
CREATE TABLE booking_request (
  id             text PRIMARY KEY,
  doctor_day_id  text NOT NULL REFERENCES doctor_day (id),
  patient_id     text NOT NULL,
  slots          integer NOT NULL CHECK (slots BETWEEN 1 AND 2),
  state          text NOT NULL CHECK (state IN ('queued', 'processing', 'done', 'rejected', 'failed')),
  reason         text,
  received_at    timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  finished_at    timestamptz,
  first_read_at  timestamptz
);

CREATE INDEX booking_request_state ON booking_request (state);

-- Спільне предметне правило обох каналів. Викликається всередині транзакції каналу, яка потім
-- записує своє бронювання; рядок doctor_day заблоковано до кінця цієї транзакції.
-- Повертає NULL, якщо слоти зайнято, інакше причину відмови.
CREATE FUNCTION take_slots(p_doctor_day_id text, p_slots integer) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  d doctor_day%ROWTYPE;
BEGIN
  SELECT * INTO d FROM doctor_day WHERE id = p_doctor_day_id FOR UPDATE;
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
