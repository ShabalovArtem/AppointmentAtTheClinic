-- DPC ЛР3 (О3.3): таблиця ефектів задач Ray. Ключ ефекту — request_id запиту кол-центру (картка C02:
-- «зайняття слоту; ключ — request_id»), однаковий у всіх спробах однієї задачі.
--
-- Приймач (POST /internal/v1/effects у сервісі api) в ОДНІЙ транзакції:
--   INSERT ... ON CONFLICT (effect_key) DO UPDATE deliveries = deliveries + 1  — захист від повтору;
--   перша доставка: take_slots() + запис booking (зміна ресурсу) і рішення в effect та booking_request;
--   повторна доставка: повертає збережене рішення, ресурс не змінює.

CREATE TABLE effect (
  effect_key        text PRIMARY KEY,                       -- = booking_request.id
  doctor_day_id     text NOT NULL REFERENCES doctor_day (id),
  patient_id        text NOT NULL,
  slots             integer NOT NULL CHECK (slots BETWEEN 1 AND 2),
  decision          text CHECK (decision IN ('done', 'rejected')),
  reason            text,
  booking_id        text,
  deliveries        integer NOT NULL DEFAULT 1 CHECK (deliveries >= 1),  -- скільки разів ефект доставлено
  first_worker      text,                                    -- вузол Ray першої доставки
  last_worker       text,
  first_at          timestamptz NOT NULL DEFAULT now(),
  last_delivery_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (decision <> 'done' OR booking_id IS NOT NULL)
);
