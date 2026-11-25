-- Web ЛР2 (О2.2): ключі ідемпотентності головної операції.
-- Рядок вставляється в ТІЙ САМІЙ транзакції, що й запис пацієнта, і в ній же отримує збережену
-- відповідь (state = 'done'). Тому ключ і предметна зміна фіксуються разом або не фіксуються взагалі.
-- Конкурентний дублікат чекає на унікальному індексі (user_id, operation, key) і за обмеженим
-- lock_timeout отримує 409 «ключ ще обробляється».

CREATE TABLE idempotency_key (
  user_id           text NOT NULL REFERENCES app_user (id),
  operation         text NOT NULL,                      -- напр. 'create_booking'
  key               text NOT NULL CHECK (char_length(key) BETWEEN 8 AND 128),
  request_hash      text NOT NULL,                      -- SHA-256 канонічного JSON тіла
  state             text NOT NULL CHECK (state IN ('processing', 'done')),
  response_status   integer,
  response_body     jsonb,
  response_headers  jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  PRIMARY KEY (user_id, operation, key),
  CHECK (state <> 'done' OR (response_status IS NOT NULL AND response_body IS NOT NULL)),
  CHECK (expires_at > created_at)
);

-- прибирання прострочених ключів порціями
CREATE INDEX idempotency_key_expires ON idempotency_key (expires_at);

-- Дні, створені лише для дослідів: не показуються в каталозі сайту й розкладі реєстратури.
ALTER TABLE doctor_day ADD COLUMN experiment boolean NOT NULL DEFAULT false;
