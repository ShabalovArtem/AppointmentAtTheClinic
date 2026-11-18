-- Демодані C02 для демонстрації та перевірок обох курсів.
-- Користувачі Web (паролі для ЛР3; до ЛР3 користувача задає заголовок X-User-Id):
--   пацієнти p-1001..p-1005 (логіни patient1..patient5 / patient-pass), реєстратор u-registrar (registrar / registrar-pass).
-- Дні лікарів:
--   dd-main          — 100 000 слотів: звичайні запити й навантаження (приклад normal обох контрактів)
--   dd-one           — 1 слот: гонка «останній слот» (приклад scarce Web, x-capacity: 1)
--   dd-small         — 16 слотів: демо кол-центру
--   dd-joint         — 10 слотів: спільний дослід «сайт + кол-центр одночасно»
--   race-locked      — 20 слотів: дослід DPC О1.4 із захистом
--   race-unprotected — 20 слотів, unprotected_test: дослід DPC О1.4 без захисту (лише тут CHECK не діє)
--   решта            — розклад для каталогу сайту.
-- Гроші — копійки (50000 = 500,00 грн за слот).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO app_user (id, login, name, password_hash, role) VALUES
  ('p-1001', 'patient1', 'Олена Коваль',     crypt('patient-pass', gen_salt('bf')), 'patient'),
  ('p-1002', 'patient2', 'Андрій Мельник',   crypt('patient-pass', gen_salt('bf')), 'patient'),
  ('p-1003', 'patient3', 'Ірина Ткаченко',   crypt('patient-pass', gen_salt('bf')), 'patient'),
  ('p-1004', 'patient4', 'Максим Бондар',    crypt('patient-pass', gen_salt('bf')), 'patient'),
  ('p-1005', 'patient5', 'Софія Шевчук',     crypt('patient-pass', gen_salt('bf')), 'patient'),
  ('u-registrar', 'registrar', 'Оксана Лисенко', crypt('registrar-pass', gen_salt('bf')), 'staff');

INSERT INTO doctor (id, full_name, specialty) VALUES
  ('doc-1', 'Іваненко Олег Петрович', 'терапевт'),
  ('doc-2', 'Петренко Марія Сергіївна', 'кардіолог'),
  ('doc-3', 'Коваль Наталія Василівна', 'педіатр'),
  ('doc-4', 'Гнатюк Тарас Ігорович', 'невролог');

INSERT INTO doctor_day (id, doctor_id, day, slots_total, price_minor, unprotected_test) VALUES
  ('dd-main',          'doc-1', '2026-12-01', 100000, 50000, false),
  ('dd-one',           'doc-2', '2026-12-03', 1,      90000, false),
  ('dd-small',         'doc-2', '2026-12-01', 16,     90000, false),
  ('dd-joint',         'doc-4', '2026-12-04', 10,     70000, false),
  ('race-locked',      'doc-3', '2026-12-01', 20,     60000, false),
  ('race-unprotected', 'doc-3', '2026-12-02', 20,     60000, true),
  ('dd-doc1-1202',     'doc-1', '2026-12-02', 32,     50000, false),
  ('dd-doc1-1203',     'doc-1', '2026-12-03', 32,     50000, false),
  ('dd-doc2-1202',     'doc-2', '2026-12-02', 24,     90000, false),
  ('dd-doc3-1203',     'doc-3', '2026-12-03', 28,     60000, false),
  ('dd-doc4-1202',     'doc-4', '2026-12-02', 20,     70000, false),
  ('dd-doc4-1203',     'doc-4', '2026-12-03', 20,     70000, false);
