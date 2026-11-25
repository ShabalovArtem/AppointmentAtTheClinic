-- Тестові дні дослідів Web ЛР2 (окремо від робочих даних):
--   web-race             — 1 слот: «20 пацієнтів на останній слот» (О2.1) через обидва екземпляри
--   web-race-unprotected — 1 слот, unprotected_test: той самий дослід у режимі INVARIANT_MODE=unprotected
--   web-compare          — 50 слотів: порівняння двох способів захисту інваріанта (Д2.2)
--   web-retry            — 1000 слотів: втрачена відповідь і повтор із тим самим ключем (О2.3)
-- Скидання: web/experiments/reset_web.sql.

INSERT INTO doctor_day (id, doctor_id, day, slots_total, price_minor, unprotected_test, experiment) VALUES
  ('web-race',             'doc-1', '2026-12-10', 1,    50000, false, true),
  ('web-race-unprotected', 'doc-1', '2026-12-11', 1,    50000, true,  true),
  ('web-compare',          'doc-1', '2026-12-12', 50,   50000, false, true),
  ('web-retry',            'doc-1', '2026-12-14', 1000, 50000, false, true);

-- тестові дні дослідів DPC і спільного досліду теж не показуються в каталозі сайту
UPDATE doctor_day SET experiment = true WHERE id IN ('race-locked', 'race-unprotected', 'dd-joint');
