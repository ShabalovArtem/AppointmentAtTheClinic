"""О2.1: «20 пацієнтів на останній слот» через обидва екземпляри.

20 зареєстрованих пацієнтів через бар'єр одночасно надсилають POST /v1/bookings (кожен зі своїм
Idempotency-Key); парні — напряму на app-a (:8001), непарні — на app-b (:8002). Після цього
читається ресурс (спільна база, маршрут частини DPC): зайнято, фактично записано, invariant_ok.

Із кореня проєкту:
    python web/experiments/lab2/race.py --mode protected                 # день web-race
    python web/experiments/lab2/race.py --mode unprotected --gap-ms 50   # день web-race-unprotected
Скрипт сам перезапускає app-a/app-b з INVARIANT_MODE і скидає тестовий день.
"""

import argparse
import sys
import threading
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["protected", "conditional", "unprotected"], default="protected")
    ap.add_argument("--patients", type=int, default=20)
    ap.add_argument("--gap-ms", type=int, default=0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    day = "web-race-unprotected" if a.mode == "unprotected" else "web-race"

    c.restart_apps({"INVARIANT_MODE": a.mode, "INVARIANT_GAP_MS": str(a.gap_ms)})
    c.reset_day(day)
    patients = c.register(a.patients, "race")
    before = c.resource(day)

    results: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(patients))

    def go(i: int, p: dict) -> None:
        base = c.APP_A if i % 2 == 0 else c.APP_B
        barrier.wait()
        s, h, d, sec = c.call(base, "POST", "/v1/bookings", {"doctor_day_id": day, "slots": 1}, token=p["token"],
                              headers={"Idempotency-Key": f"race-{uuid.uuid4()}"})
        with lock:
            results.append({"patient": p["id"], "instance": h.get("x-instance"), "status": s,
                            "type": (d or {}).get("type", "").rsplit("/", 1)[-1] if s >= 400 else None,
                            "ms": round(sec * 1000, 1)})

    threads = [threading.Thread(target=go, args=(i, p)) for i, p in enumerate(patients)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    after = c.resource(day)

    summary = {
        "mode": a.mode, "gap_ms": a.gap_ms, "doctor_day": day, "patients": len(patients),
        "statuses": dict(Counter(r["status"] for r in results)),
        "by_instance": dict(Counter(f"{r['instance']}:{r['status']}" for r in results)),
        "refusal_types": dict(Counter(r["type"] for r in results if r["type"])),
        "resource_before": {k: before[k] for k in ("slots_total", "slots_taken", "booked")},
        "resource_after": {k: after[k] for k in ("slots_total", "slots_taken", "booked", "invariant_ok")},
        "requests": results,
    }
    path = c.save(a.out or f"web/results/lab2/race-{a.mode}", "summary.json", summary)
    print(f"mode={a.mode} statuses={summary['statuses']} by_instance={summary['by_instance']}")
    print(f"after: slots_total={after['slots_total']} slots_taken={after['slots_taken']} booked={after['booked']} "
          f"invariant_ok={after['invariant_ok']} -> {path}")
    if a.mode != "protected":
        c.restart_apps({"INVARIANT_MODE": "protected", "INVARIANT_GAP_MS": "0"})  # повернути робочий режим
    return 0 if after["invariant_ok"] or a.mode == "unprotected" else 1


if __name__ == "__main__":
    sys.exit(main())
