-- Демодані C02. dd-main — день лікаря для перевірки й навантаження (слотів вистачає на всі запити);
-- dd-small — невеликий день; race-* — окремі тестові ресурси для досліду О1.4
-- (race-unprotected запускається лише з RESOURCE_MODE=unprotected).

INSERT INTO doctor (id, full_name, specialty) VALUES
  ('doc-1', 'Іваненко О. П.', 'терапевт'),
  ('doc-2', 'Петренко М. С.', 'кардіолог'),
  ('doc-3', 'Коваль Н. В.', 'педіатр');

INSERT INTO doctor_day (id, doctor_id, day, slots_total) VALUES
  ('dd-main',          'doc-1', '2026-12-01', 100000),
  ('dd-small',         'doc-2', '2026-12-01', 16),
  ('race-locked',      'doc-3', '2026-12-01', 20),
  ('race-unprotected', 'doc-3', '2026-12-02', 20);
