"""О3.2 (межа незавершених задач) і Д3.4 (повільний приймач і зворотний тиск).

Для кожної затримки приймача RECEIVER_DELAY_MS (типово 200 і 1000 мс) api перезапускається з EXECUTION=ray,
IN_FLIGHT_LIMIT=8, QUEUE_CAPACITY=50, повільний виклик 200–800 мс; інші умови однакові: 12 клієнтів 20 с без пауз
подають запити на dd-main (keep-alive), окремий потік кожні 100 мс читає /metrics (ray_in_flight, queue_length).
Перед навантаженням — прогрів (16 запитів, не враховуються). Після навантаження — очікування спорожнення, читання часу повного виконання кожного прийнятого запиту.

Вимірюється: максимум незавершених задач Ray (має бути ≤ IN_FLIGHT_LIMIT), максимум довжини черги, пропускна
здатність (запитів/с), p95 повного виконання (надходження → результат, мс), частка 429.

Із кореня проєкту:  python DPC/experiments/lab3/backpressure.py --delays 200,1000
"""

import argparse
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab3common as c  # noqa: E402

sys.path.insert(0, str(c.ROOT / "DPC" / "experiments" / "lab1"))
from common import Api, percentile  # noqa: E402  (keep-alive HTTP із ЛР1)


def run(delay_ms: int, clients: int, seconds: float) -> dict:
    c.restart_api({"EXECUTION": "ray", "RECEIVER_DELAY_MS": str(delay_ms), "IN_FLIGHT_LIMIT": "8", "QUEUE_CAPACITY": "50"})
    api = Api(c.API, c.TOKEN)
    # прогрів: після перезапуску api Ray запускає нові процеси-виконавці для нового завдання (job);
    # 16 запитів до початку вимірювання, щоб холодний старт не потрапив у виміри
    warm = [f"warm-{uuid.uuid4().hex[:10]}" for _ in range(16)]
    for rid in warm:
        api.call("POST", "/v1/booking-requests", {"request_id": rid, "doctor_day_id": "dd-main", "patient_id": f"cc-{rid}", "slots": 1})
    for rid in warm:
        c.wait_final(rid)
    while (api.call("GET", "/metrics")[2] or {"ray_in_flight": 1}).get("ray_in_flight"):
        time.sleep(0.2)
    stop = threading.Event()
    lock = threading.Lock()
    submits, samples = [], []
    t0 = time.monotonic()

    def client(n):
        while not stop.is_set():
            rid = f"bp-{uuid.uuid4().hex[:12]}"
            s, h, _, _ = api.call("POST", "/v1/booking-requests",
                                  {"request_id": rid, "doctor_day_id": "dd-main", "patient_id": f"cc-{rid}", "slots": 1})
            with lock:
                submits.append((rid, s))

    def sampler():
        while not stop.is_set():
            s, _, d, _ = api.call("GET", "/metrics")
            if s == 200 and d:
                samples.append({"t": round(time.monotonic() - t0, 2), "in_flight": d.get("ray_in_flight"),
                                "queue": d.get("queue_length"), "submitted": d.get("ray_submitted")})
            time.sleep(0.1)

    threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(clients)]
    threads.append(threading.Thread(target=sampler, daemon=True))
    [t.start() for t in threads]
    time.sleep(seconds)
    stop.set()
    [t.join() for t in threads]
    while True:  # спорожнення черги й завершення задач
        _, _, d, _ = api.call("GET", "/metrics")
        if not d:
            time.sleep(0.5)
            continue
        samples.append({"t": round(time.monotonic() - t0, 2), "in_flight": d.get("ray_in_flight"),
                        "queue": d.get("queue_length"), "submitted": d.get("ray_submitted")})
        if d.get("queue_length") == 0 and d.get("ray_in_flight") == 0:
            break
        time.sleep(0.2)
    drained = time.monotonic() - t0
    _, _, m, _ = api.call("GET", "/metrics")
    accepted = [rid for rid, s in submits if s == 202]
    refused = sum(1 for _, s in submits if s == 429)
    totals, states = [], {}
    for rid in accepted:
        s, _, d, _ = api.call("GET", f"/v1/booking-requests/{rid}")
        states[d["state"]] = states.get(d["state"], 0) + 1
        if d.get("timing") and d["timing"].get("total_ms") is not None:
            totals.append(d["timing"]["total_ms"])
    return {
        "receiver_delay_ms": delay_ms, "clients": clients, "load_seconds": seconds, "in_flight_limit": 8,
        "queue_capacity": 50, "submits": len(submits), "accepted": len(accepted), "refused_429": refused,
        "share_429": refused / len(submits) if submits else 0, "final_states": states,
        "throughput_per_s": len(accepted) / drained, "drained_after_s": round(drained, 1),
        "failed": states.get("failed", 0),
        "full_execution_ms": {"p50": percentile(totals, 50), "p95": percentile(totals, 95), "n": len(totals)},
        "max_in_flight_sampled": max(s["in_flight"] or 0 for s in samples),
        "max_in_flight_server": m.get("ray_max_in_flight"), "ray_submitted": m.get("ray_submitted"),
        "max_queue": max(s["queue"] or 0 for s in samples), "samples": samples,
    }


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--delays", default="200,1000")
    ap.add_argument("--clients", type=int, default=12)
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    out = {"runs": [run(int(d), a.clients, a.seconds) for d in a.delays.split(",")]}
    c.restart_api({"EXECUTION": "ray", "RECEIVER_DELAY_MS": "0", "IN_FLIGHT_LIMIT": "8", "QUEUE_CAPACITY": "50"})
    for r in out["runs"]:
        print(f"delay={r['receiver_delay_ms']:>5} ms  accepted={r['accepted']:>4}  429={r['share_429']:.1%}  "
              f"throughput={r['throughput_per_s']:.2f}/s  p95 full={r['full_execution_ms']['p95']} ms  "
              f"max in-flight={r['max_in_flight_server']} (limit 8)  max queue={r['max_queue']}  submitted={r['ray_submitted']}")
    ok = all(r["max_in_flight_server"] <= 8 and r["ray_submitted"] == r["accepted"] + 16 for r in out["runs"])
    out["ok"] = ok
    path = c.save("DPC/results/lab3/backpressure", "summary.json", out)
    print(f"{'OK' if ok else 'FAIL'} -> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
