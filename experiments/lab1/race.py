"""Дослід О1.4: той самий сценарій із захистом ресурсу й без нього.

REQUESTS запитів по 1 слоту одночасно подаються на день лікаря зі SLOTS_TOTAL=20 слотами
(тестові ресурси race-locked / race-unprotected). Після завершення всіх запитів читається ресурс:
інваріант — slots_taken = сума слотів у booking і не більший за slots_total.

Ресурс перед кожним запуском скидається:
    docker compose exec -T postgres psql -U app -d app -f - < experiments/lab1/reset.sql
Із кореня проєкту:
    python experiments/lab1/race.py --doctor-day race-locked --out results/lab1/race-locked
"""

import argparse
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import Api, dump, out_dir  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--doctor-day", default="race-locked")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    api = Api(args.base, args.token)
    target = out_dir(args.out or f"results/lab1/{args.doctor_day}")
    _, _, before, _ = api.call("GET", f"/v1/doctor-day/{args.doctor_day}")
    _, _, metrics, _ = api.call("GET", "/metrics")
    if not before or before["slots_taken"] != 0:
        print(f"resource is not reset: {before}; run experiments/lab1/reset.sql first")
        return 2

    tag = uuid.uuid4().hex[:8]
    ids = [f"race-{tag}-{i:03d}" for i in range(args.requests)]
    statuses: list[int] = []
    barrier = threading.Barrier(args.requests)

    def submit(i: int) -> None:
        barrier.wait()  # усі запити стартують одночасно
        body = {"request_id": ids[i], "doctor_day_id": args.doctor_day, "patient_id": f"p-{i}", "slots": 1}
        statuses.append(api.call("POST", "/v1/booking-requests", body)[0])

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(args.requests)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    finals = [api.wait_final(rid) for rid in ids]
    elapsed = time.monotonic() - started
    _, _, after, _ = api.call("GET", f"/v1/doctor-day/{args.doctor_day}")

    summary = {
        "resource_mode": metrics.get("resource_mode"),
        "workers": metrics.get("workers"),
        "queue_capacity": metrics.get("queue_capacity"),
        "requests": args.requests,
        "submit_statuses": dict(Counter(statuses)),
        "final_states": dict(Counter(f["state"] if f else "timeout" for f in finals)),
        "reasons": dict(Counter(f["reason"] for f in finals if f and f["reason"])),
        "seconds": round(elapsed, 2),
        "resource_before": before,
        "resource_after": after,
    }
    dump(target, "summary.json", summary)
    print(f"mode={summary['resource_mode']} finals={summary['final_states']}")
    print(f"slots_total={after['slots_total']} slots_taken={after['slots_taken']} booked={after['booked']} "
          f"invariant_ok={after['invariant_ok']}; results in {target}")
    return 0 if after["invariant_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
