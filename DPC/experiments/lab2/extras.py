"""Д2.5 (паралельна гістограма запізнень) і Д2.6 (скасування асинхронного розрахунку).

Д2.5: GET /v1/histogram для 1, 2 і 4 процесів (n = 2·10^6, seed 42) і незалежний послідовний розрахунок у цьому
      скрипті тією самою функцією late_minutes (значення залежить від номера пацієнта, а не процесу).
      Гістограми мають збігтися точно (цілі лічильники).
Д2.6: асинхронний розрахунок n = 3·10^8 на 4 процесах (кілька секунд); через 1 с — DELETE; далі GET кожні 50 мс,
      доки active_parts не стане 0. Очікування: ≤ 1 с від запиту скасування, стан cancelled, результату немає.

Із кореня проєкту:  python DPC/experiments/lab2/extras.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

sys.path.insert(0, str(c.ROOT / "DPC" / "api" / "app"))
import integrate  # noqa: E402  (легкий модуль: лише math, os, time)


def main() -> int:
    c.utf8_stdout()
    out: dict = {}
    n, seed = 2_000_000, 42
    started = time.perf_counter()
    sequential = {b: 0 for b in integrate.LATE_BINS}
    for i in range(n):
        sequential[integrate.late_bin(integrate.late_minutes(seed, i))] += 1
    seq_s = time.perf_counter() - started
    hist = {}
    for p in (1, 2, 4):
        status, data, _ = c.call("GET", f"/v1/histogram?n={n}&workers={p}&seed={seed}")
        hist[p] = data
    equal = all(hist[p]["histogram"] == sequential for p in hist)
    out["histogram"] = {"n": n, "seed": seed, "sequential_local_s": seq_s, "sequential": sequential,
                        "by_workers": {p: {"histogram": h["histogram"], "wall_s": h["wall_s"]} for p, h in hist.items()},
                        "equal_to_sequential": equal}
    print("Д2.5 histogram of lateness, n=2e6:")
    for b in integrate.LATE_BINS:
        print(f"  {b:>6}: {sequential[b]:>8}  " + "  ".join(f"p{p}={hist[p]['histogram'][b]}" for p in hist))
    print(f"  equal to sequential for p=1,2,4: {equal}; wall " + ", ".join(f"p{p}={hist[p]['wall_s']:.2f}s" for p in hist))

    status, job, _ = c.call("POST", "/v1/compute", {"n": 300_000_000, "workers": 4, "async": True})
    assert status == 202, (status, job)
    time.sleep(1.0)
    s1, before, _ = c.call("GET", f"/v1/compute/{job['id']}")
    t_cancel = time.perf_counter()
    s2, after_delete, _ = c.call("DELETE", f"/v1/compute/{job['id']}")
    trace = []
    while True:
        s, cur, _ = c.call("GET", f"/v1/compute/{job['id']}")
        trace.append({"t": round(time.perf_counter() - t_cancel, 3), "state": cur["state"], "active_parts": cur["active_parts"]})
        if cur["active_parts"] == 0 and cur["state"] in ("cancelled", "done", "failed"):
            break
        if time.perf_counter() - t_cancel > 10:
            break
        time.sleep(0.05)
    zero_at = next((x["t"] for x in trace if x["active_parts"] == 0), None)
    ok_cancel = cur["state"] == "cancelled" and cur["result"] is None and zero_at is not None and zero_at <= 1.0
    out["cancel"] = {"before": {"state": before["state"], "active_parts": before["active_parts"]},
                     "delete_status": s2, "trace": trace, "active_parts_zero_after_s": zero_at,
                     "final": {"state": cur["state"], "result": cur["result"]}, "ok": ok_cancel}
    print(f"\nД2.6 cancel: before={before['state']}/{before['active_parts']} parts; DELETE -> {s2}; "
          f"active_parts=0 after {zero_at} s; final state={cur['state']}, result={cur['result']}")
    path = c.save("DPC/results/lab2/extras", "summary.json", out)
    ok = equal and ok_cancel
    print(f"{'OK' if ok else 'FAIL'} -> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
