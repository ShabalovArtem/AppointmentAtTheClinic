-- Вхід, реєстрація пацієнтів і скасування записів (частина Web).

-- Сесії входу (user_session): у базі лише SHA-256 від токена, тож витік таблиці не дає чинних токенів.
-- Стан сесії в базі — однаковий для app-a і app-b.
CREATE TABLE user_session (
  token_hash  text PRIMARY KEY,
  user_id     text NOT NULL REFERENCES app_user (id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  CHECK (expires_at > created_at)
);
CREATE INDEX user_session_by_user ON user_session (user_id);

-- Логін: латиниця, цифри, крапка, дефіс, підкреслення; без урахування регістру — один користувач.
ALTER TABLE app_user ADD CONSTRAINT app_user_login_format CHECK (login ~ '^[a-z0-9._-]{3,40}$');
ALTER TABLE app_user ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();

-- Чому й коли запис скасовано (пацієнт, закриття дня реєстратором).
ALTER TABLE booking ADD COLUMN cancelled_at timestamptz;
ALTER TABLE booking ADD COLUMN cancel_reason text CHECK (cancel_reason IN ('patient', 'day-closed'));
ALTER TABLE booking ADD CONSTRAINT booking_cancel_consistent
  CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL));

-- Парна до take_slots(): повертає слоти дня. Викликається в транзакції, що переводить запис
-- зі стану, який займав слоти, у cancelled (або expired), — рівно один раз на запис.
CREATE FUNCTION release_slots(p_doctor_day_id text, p_slots integer) RETURNS void
LANGUAGE sql AS $$
  UPDATE doctor_day SET slots_taken = slots_taken - p_slots WHERE id = p_doctor_day_id;
$$;
