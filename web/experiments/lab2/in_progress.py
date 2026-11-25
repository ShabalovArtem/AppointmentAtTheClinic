"""О2.2: «ключ ще обробляється — 409».

Щоб перша транзакція тривала помітно довго, app-a/app-b запускаються з INVARIANT_MODE=unprotected і
INVARIANT_GAP_MS=600 (пауза всередині транзакції; діє лише на тестовому дні web-race-unprotected), а межа
очікування дубліката IDEMPOTENCY_WAIT_MS=50. Перший запит із ключем K іде на app-a, через 150 мс той
самий запит із K — на app-b: він не може зайняти ключ за 50 мс і отримує 409 з Retry-After. Після
завершення першого — повтор із K повертає збережену відповідь 201. Запис — один.

Із кореня проєкту:  python web/experiments/lab2/in_progress.py
"""

import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

DAY = "web-race-unprotected"


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="web/results/lab2/in-progress")
    a = ap.parse_args()
    c.restart_apps({"INVARIANT_MODE": "unprotected", "INVARIANT_GAP_MS": "600", "IDEMPOTENCY_WAIT_MS": "50"})
    c.reset_day(DAY)
    patient = c.register(1, "inprog")[0]
    key = f"inprog-{uuid.uuid4()}"
    body = {"doctor_day_id": DAY, "slots": 1}
    first = {}

    def send_first():
        first["r"] = c.call(c.APP_A, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})

    t = threading.Thread(target=send_first)
    t.start()
    time.sleep(0.15)
    s2, h2, d2, sec2 = c.call(c.APP_B, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})
    t.join()
    s1, h1, d1, sec1 = first["r"]
    s3, h3, d3, _ = c.call(c.APP_B, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})
    count = int(c.psql(f"SELECT count(*) FROM booking WHERE patient_id = '{patient['id']}'"))
    c.restart_apps({"INVARIANT_MODE": "protected", "INVARIANT_GAP_MS": "0", "IDEMPOTENCY_WAIT_MS": "200"})

    summary = {
        "first_app_a": {"status": s1, "id": (d1 or {}).get("id"), "seconds": round(sec1, 3)},
        "duplicate_while_processing_app_b": {"status": s2, "type": (d2 or {}).get("type"), "retry_after": h2.get("retry-after"),
                                             "seconds": round(sec2, 3)},
        "repeat_after_commit_app_b": {"status": s3, "id": (d3 or {}).get("id"), "replayed": h3.get("idempotent-replayed")},
        "bookings_of_patient": count,
    }
    ok = (s1 == 201 and s2 == 409 and str((d2 or {}).get("type", "")).endswith("idempotency-key-in-progress")
          and h2.get("retry-after") and s3 == 201 and (d3 or {}).get("id") == (d1 or {}).get("id") and count == 1)
    summary["ok"] = ok
    path = c.save(a.out, "summary.json", summary)
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"-> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
