"""Д2.2: два способи забезпечення інваріанта під однаковим навантаженням.

protected   — функція take_slots(): SELECT ... FOR UPDATE (блокування рядка) + перевірка + UPDATE;
conditional — один атомарний умовний UPDATE ... WHERE slots_taken + n <= slots_total.
Однакове навантаження: день web-compare (50 слотів), 100 різних пацієнтів одночасно через nginx
(обидва екземпляри), по 1 слоту; ROUNDS повторів на кожен режим. Вимірюється час HTTP-відповіді
POST /v1/bookings на клієнті (p50/p95) і тривалість раунду; перевіряється підсумковий стан ресурсу.

Із кореня проєкту:  python web/experiments/lab2/compare.py --rounds 5
"""

import argparse
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

DAY = "web-compare"


def run_round(patients):
    c.reset_day(DAY)
    lat, statuses = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(len(patients) + 1)

    def go(p):
        barrier.wait()
        s, _, _, sec = c.call(c.NGINX, "POST", "/v1/bookings", {"doctor_day_id": DAY, "slots": 1}, token=p["token"],
                              headers={"Idempotency-Key": f"cmp-{uuid.uuid4()}"})
        with lock:
            lat.append(sec * 1000)
            statuses.append(s)

    threads = [threading.Thread(target=go, args=(p,)) for p in patients]
    [t.start() for t in threads]
    barrier.wait()
    started = time.perf_counter()
    [t.join() for t in threads]
    wall = (time.perf_counter() - started) * 1000
    res = c.resource(DAY)
    return {"wall_ms": round(wall, 1), "p50_ms": round(c.percentile(lat, 50), 1), "p95_ms": round(c.percentile(lat, 95), 1),
            "statuses": dict(Counter(statuses)), "slots_taken": res["slots_taken"], "booked": res["booked"],
            "invariant_ok": res["invariant_ok"]}


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--patients", type=int, default=100)
    ap.add_argument("--out", default="web/results/lab2/compare")
    a = ap.parse_args()

    summary = {"doctor_day": DAY, "slots_total": 50, "patients": a.patients, "rounds": a.rounds, "modes": {}}
    patients = None
    for mode in ("protected", "conditional"):
        c.restart_apps({"INVARIANT_MODE": mode, "INVARIANT_GAP_MS": "0"})
        if patients is None:
            patients = c.register(a.patients, "cmp")
        run_round(patients)  # прогрів пулів з'єднань, не враховується
        rounds = [run_round(patients) for _ in range(a.rounds)]
        summary["modes"][mode] = {
            "rounds": rounds,
            "median_wall_ms": round(statistics.median(r["wall_ms"] for r in rounds), 1),
            "median_p50_ms": round(statistics.median(r["p50_ms"] for r in rounds), 1),
            "median_p95_ms": round(statistics.median(r["p95_ms"] for r in rounds), 1),
            "all_rounds_invariant_ok": all(r["invariant_ok"] and r["booked"] == 50 and r["statuses"].get(201) == 50
                                           for r in rounds),
        }
        m = summary["modes"][mode]
        print(f"{mode:12s} wall={m['median_wall_ms']} ms p50={m['median_p50_ms']} ms p95={m['median_p95_ms']} ms "
              f"invariant_ok={m['all_rounds_invariant_ok']} statuses={rounds[0]['statuses']}")
    c.restart_apps({"INVARIANT_MODE": "protected", "INVARIANT_GAP_MS": "0"})
    path = c.save(a.out, "summary.json", summary)
    print(f"-> {path}")
    return 0 if all(m["all_rounds_invariant_ok"] for m in summary["modes"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
