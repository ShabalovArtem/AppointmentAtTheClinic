"""Дослід О1.2 + О1.5: заповнена черга, 429 з Retry-After, затримка /health, час виконання.

CLIENTS потоків безперервно подають запити на запис (без пауз, Retry-After навмисно ігнорують,
щоб тримати чергу повною). Окремий потік кожні HEALTH_EVERY с вимірює час HTTP-відповіді /health,
ще один кожні 0,25 с читає /metrics (довжина черги, обробники). Після SECONDS с подання
припиняється, скрипт чекає спорожнення черги, читає кінцевий стан кожного прийнятого запиту
(це перша видача — позначка first_read) і перевіряє, що відхилені 429 запити не потрапили в систему.

Із кореня проєкту:
    python experiments/lab1/queue_full.py --clients 16 --seconds 30 --out results/lab1/queue-full
"""

import argparse
import csv
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import Api, dump, out_dir, percentile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--doctor-day", default="dd-main")
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--health-every", type=float, default=0.05)
    parser.add_argument("--out", default="results/lab1/queue-full")
    args = parser.parse_args()

    api = Api(args.base, args.token)
    target = out_dir(args.out)
    stop = threading.Event()
    lock = threading.Lock()
    submits: list[dict] = []
    health: list[dict] = []
    metric_samples: list[dict] = []
    t0 = time.monotonic()

    def client(n: int) -> None:
        while not stop.is_set():
            rid = f"q-{uuid.uuid4().hex[:16]}"
            body = {"request_id": rid, "doctor_day_id": args.doctor_day, "patient_id": f"p-{n}", "slots": 1}
            status, headers, _, sec = api.call("POST", "/v1/booking-requests", body)
            with lock:
                submits.append({
                    "t": round(time.monotonic() - t0, 3), "client": n, "request_id": rid, "status": status,
                    "retry_after": headers.get("retry-after", ""), "ms": round(sec * 1000, 2),
                })

    def health_probe() -> None:
        while not stop.is_set():
            status, _, _, sec = api.call("GET", "/health")
            health.append({"t": round(time.monotonic() - t0, 3), "status": status, "ms": round(sec * 1000, 2)})
            time.sleep(args.health_every)

    def metrics_probe() -> None:
        while not stop.is_set():
            status, _, data, _ = api.call("GET", "/metrics")
            if status == 200 and data:
                metric_samples.append({
                    "t": round(time.monotonic() - t0, 3), "queue_length": data.get("queue_length"),
                    "processing": data.get("processing"), "rejected": data.get("rejected"),
                })
            time.sleep(0.25)

    threads = [threading.Thread(target=client, args=(n,), daemon=True) for n in range(args.clients)]
    threads += [threading.Thread(target=health_probe, daemon=True), threading.Thread(target=metrics_probe, daemon=True)]
    for t in threads:
        t.start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join()
    load_s = time.monotonic() - t0

    # дочекатися спорожнення черги та завершення обробки
    while True:
        _, _, data, _ = api.call("GET", "/metrics")
        if data and data.get("queue_length") == 0 and data.get("processing") == 0:
            break
        time.sleep(0.5)

    accepted = [s for s in submits if s["status"] == 202]
    refused = [s for s in submits if s["status"] == 429]
    finals: dict[str, int] = {}
    timing_rows = []
    for s in accepted:  # перша видача кінцевого стану
        status, _, data, _ = api.call("GET", f"/v1/booking-requests/{s['request_id']}")
        state = data["state"] if status == 200 and data else f"http-{status}"
        finals[state] = finals.get(state, 0) + 1
        if data and data.get("timing"):
            timing_rows.append({"request_id": s["request_id"], **data["timing"]})
    leaked = 0
    for s in refused[:500]:  # 429 => запит не прийнято: його немає в системі
        status, _, _, _ = api.call("GET", f"/v1/booking-requests/{s['request_id']}")
        leaked += status != 404
    _, _, metrics_after, _ = api.call("GET", "/metrics")
    _, _, resource, _ = api.call("GET", f"/v1/doctor-day/{args.doctor_day}")

    for name, rows in (("requests.csv", submits), ("health.csv", health), ("queue.csv", metric_samples),
                       ("timing.csv", timing_rows)):
        if rows:
            with (target / name).open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

    health_ms = [h["ms"] for h in health if h["status"] == 200]
    summary = {
        "params": {"clients": args.clients, "seconds": args.seconds, "doctor_day": args.doctor_day,
                   "queue_capacity": metrics_after.get("queue_capacity"), "workers": metrics_after.get("workers")},
        "load_seconds": round(load_s, 1),
        "submits": len(submits),
        "statuses": {str(k): sum(1 for s in submits if s["status"] == k) for k in sorted({s["status"] for s in submits})},
        "all_429_have_retry_after": all(s["retry_after"] for s in refused),
        "refused_checked": min(len(refused), 500),
        "refused_found_in_system": leaked,
        "max_queue_length": max((m["queue_length"] or 0 for m in metric_samples), default=None),
        "max_processing": max((m["processing"] or 0 for m in metric_samples), default=None),
        "health": {"n": len(health), "errors": sum(1 for h in health if h["status"] != 200),
                   "p50_ms": percentile(health_ms, 50), "p95_ms": percentile(health_ms, 95),
                   "p99_ms": percentile(health_ms, 99), "max_ms": max(health_ms, default=None)},
        "accepted_final_states": finals,
        # вибірка — усі прийняті запити цього прогону; інтервали з монотонного годинника сервера
        "timing_this_run_ms": {
            name: {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95), "p99": percentile(vals, 99)}
            for name in ("queue_wait_ms", "processing_ms", "total_ms", "delivery_wait_ms")
            for vals in [[r[name] for r in timing_rows if r.get(name) is not None]]
        },
        "server_metrics_after": metrics_after,
        "resource_after": resource,
    }
    dump(target, "summary.json", summary)
    print(f"submits={summary['submits']} statuses={summary['statuses']} max_queue={summary['max_queue_length']}")
    print(f"/health p95={summary['health']['p95_ms']} ms (n={len(health_ms)}); finals={finals}; leaked={leaked}")
    print(f"timing_this_run_ms={summary['timing_this_run_ms']}")
    print(f"invariant_ok={resource.get('invariant_ok') if resource else None}; results in {target}")
    ok = summary["health"]["p95_ms"] is not None and summary["health"]["p95_ms"] <= 100 and leaked == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
