"""О2.3: повтор після втраченої відповіді (керована відмова after_commit_before_response).

1. Вмикається відмова: файл /faults/after_commit_before_response у спільному томі faults.
2. Пацієнт надсилає POST /v1/bookings із ключем K напряму на app-a: транзакцію зафіксовано, а процес
   завершується до відповіді — клієнт бачить обрив з'єднання (status 0). Docker перезапускає app-a.
3. Той самий запит із тим самим K — через app-b: очікується збережена відповідь (201, той самий id,
   Idempotent-Replayed: true), а запис пацієнта існує рівно один, слоти зайнято один раз.

Із кореня проєкту:  python web/experiments/lab2/lost_response.py
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

DAY = "web-retry"


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="web/results/lab2/lost-response")
    a = ap.parse_args()

    c.wait_ready()
    c.reset_day(DAY)
    patient = c.register(1, "lost")[0]
    before = c.resource(DAY)
    key = f"lost-{uuid.uuid4()}"
    body = {"doctor_day_id": DAY, "slots": 2, "visit_reason": "Повтор після обриву"}

    c.compose("exec", "-T", "app-a", "touch", "/faults/after_commit_before_response")
    s1, _, d1, t1 = c.call(c.APP_A, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})
    after_crash = c.resource(DAY)
    time.sleep(1)
    s2, h2, d2, t2 = c.call(c.APP_B, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})
    # третій повтор — знову через app-a, коли Docker її перезапустить
    c.wait_ready()
    s3, h3, d3, _ = c.call(c.APP_A, "POST", "/v1/bookings", body, token=patient["token"], headers={"Idempotency-Key": key})
    after = c.resource(DAY)
    count = int(c.psql(f"SELECT count(*) FROM booking WHERE patient_id = '{patient['id']}' AND doctor_day_id = '{DAY}'"))
    logs = c.compose("logs", "app-a", "--since", "5m", check=False)
    hit = [line for line in logs.splitlines() if "fault.hit" in line]

    summary = {
        "key": key,
        "attempt_1_app_a": {"status": s1, "note": "0 = з'єднання обірвано, відповіді немає", "seconds": round(t1, 3)},
        "after_crash": {k: after_crash[k] for k in ("slots_taken", "booked")},
        "attempt_2_app_b": {"status": s2, "id": (d2 or {}).get("id"), "instance": h2.get("x-instance"),
                            "replayed": h2.get("idempotent-replayed")},
        "attempt_3_app_a_restarted": {"status": s3, "id": (d3 or {}).get("id"), "instance": h3.get("x-instance"),
                                      "replayed": h3.get("idempotent-replayed")},
        "resource_before": {k: before[k] for k in ("slots_taken", "booked")},
        "resource_after": {k: after[k] for k in ("slots_taken", "booked", "invariant_ok")},
        "bookings_of_patient": count,
        "fault_hit_log": hit[-1] if hit else None,
    }
    ok = (s1 == 0 and s2 == 201 and h2.get("idempotent-replayed") == "true" and s3 == 201
          and (d2 or {}).get("id") == (d3 or {}).get("id") and count == 1
          and after["slots_taken"] - before["slots_taken"] == 2 and bool(hit))
    summary["ok"] = ok
    path = c.save(a.out, "summary.json", summary)
    for k in ("attempt_1_app_a", "after_crash", "attempt_2_app_b", "attempt_3_app_a_restarted", "bookings_of_patient"):
        print(f"{k}: {summary[k]}")
    print(f"slots_taken {before['slots_taken']} -> {after['slots_taken']}; fault.hit: {bool(hit)}; {'OK' if ok else 'FAIL'} -> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
