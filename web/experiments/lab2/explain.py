"""Д2.7: план запиту списку «Мої записи» до й після індексу на 10^5 записах.

1. Генерується 100 000 записів головної сутності (booking) для 1 000 синтетичних пацієнтів gen-0000..gen-0999
   (стан cancelled, тож ресурс і інваріанти не змінюються).
2. Видаляється індекс booking_patient_newest, ANALYZE, EXPLAIN (ANALYZE, BUFFERS) запиту списку.
3. Індекс створюється знову, ANALYZE, той самий EXPLAIN. Послідовне сканування не вимикається.
4. Синтетичні записи видаляються.

Запит — той самий, що виконує GET /v1/bookings (перша сторінка, limit 20 -> LIMIT 21) для пацієнта gen-0042.
Із кореня проєкту:  python web/experiments/lab2/explain.py
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

QUERY = """EXPLAIN (ANALYZE, BUFFERS)
SELECT b.id, b.channel, b.patient_id, b.doctor_day_id, b.slots, b.visit_reason, b.status, b.version,
       b.created_at, b.expires_at, b.cancelled_at, b.cancel_reason, d.day, d.clinic_tz, d.price_minor, d.currency,
       doc.id AS doctor_id, doc.full_name AS doctor_name, doc.specialty
FROM booking b JOIN doctor_day d ON d.id = b.doctor_day_id JOIN doctor doc ON doc.id = d.doctor_id
WHERE b.patient_id = 'gen-0042' ORDER BY b.created_at DESC, b.id DESC LIMIT 21;"""


def timing(plan: str) -> dict:
    get = lambda pat: (m.group(1) if (m := re.search(pat, plan)) else None)  # noqa: E731
    return {"execution_ms": float(get(r"Execution Time: ([\d.]+) ms")), "planning_ms": float(get(r"Planning Time: ([\d.]+) ms")),
            "top_scan": next((l.strip() for l in plan.splitlines() if "Scan" in l), None),
            "shared_buffers": get(r"Buffers: (shared [^\n]+)")}


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--out", default="web/results/lab2/explain")
    a = ap.parse_args()

    c.psql("DELETE FROM booking WHERE patient_id LIKE 'gen-%';")
    c.psql(f"""INSERT INTO booking (id, channel, patient_id, doctor_day_id, slots, status, created_at, cancelled_at, cancel_reason)
SELECT 'gen-bk-' || g, 'web', 'gen-' || lpad((g % 1000)::text, 4, '0'),
       (ARRAY['dd-doc1-1202','dd-doc1-1203','dd-doc2-1202','dd-doc3-1203','dd-doc4-1202'])[1 + g % 5],
       1 + g % 2, 'cancelled', now() - make_interval(secs => g * 37), now(), 'patient'
FROM generate_series(1, {a.rows}) g;""")
    total = int(c.psql("SELECT count(*) FROM booking"))
    per_patient = int(c.psql("SELECT count(*) FROM booking WHERE patient_id = 'gen-0042'"))

    c.psql("DROP INDEX booking_patient_newest; ANALYZE booking;")
    before = c.psql(QUERY)
    c.psql("CREATE INDEX booking_patient_newest ON booking (patient_id, created_at DESC, id DESC); ANALYZE booking;")
    after = c.psql(QUERY)
    c.psql("DELETE FROM booking WHERE patient_id LIKE 'gen-%'; ANALYZE booking;")

    out = c.ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan_without_index.txt").write_text(before + "\n", encoding="utf-8")
    (out / "plan_with_index.txt").write_text(after + "\n", encoding="utf-8")
    summary = {"rows_in_booking": total, "rows_for_patient": per_patient, "without_index": timing(before),
               "with_index": timing(after)}
    c.save(a.out, "summary.json", summary)
    print(f"rows={total}, rows for gen-0042={per_patient}")
    print("without index:", summary["without_index"])
    print("with index:   ", summary["with_index"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
