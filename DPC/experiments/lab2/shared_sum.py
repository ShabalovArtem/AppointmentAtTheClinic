"""О2.4: спільна сума без синхронізації, з блокуванням і локальні суми.

Однакова задача: Q(t) на [0; 12], середні прямокутники, n = 4·10^6, 4 процеси, порція 100 одиниць —
тобто ~40 000 додавань до спільної суми. Кожна частина окремо рахує, скільки вона ДОДАЛА (contributed),
тож втрата оновлень = Σ contributed − підсумок спільної змінної.
  none  — ctx.RawValue без блокування: `shared.value += x` (прочитати → додати → записати) губить оновлення;
  lock  — ctx.Value із блокуванням навколо кожного додавання;
  local — кожна частина має локальну суму, батьківський процес об'єднує результати.
Кожен режим — REPEATS разів.

Із кореня проєкту:  python DPC/experiments/lab2/shared_sum.py
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4_000_000)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()
    out = {"n": a.n, "workers": 4, "chunk": a.chunk, "modes": {}}
    for mode in ("none", "lock", "local"):
        runs = [c.compute(n=a.n, workers=4, method="midpoint", mode=mode, chunk=a.chunk) for _ in range(a.repeats)]
        out["modes"][mode] = {
            "runs": [{"value": r["value"], "relative_error": r["relative_error"], "within_tolerance": r["within_tolerance"],
                      "wall_s": r["wall_s"], "lost_update": r.get("lost_update")} for r in runs],
            "median_wall_s": statistics.median(r["wall_s"] for r in runs),
            "all_within_tolerance": all(r["within_tolerance"] for r in runs),
        }
        print(f"\nmode={mode}")
        for r in runs:
            lu = r.get("lost_update")
            lost = f"  lost {lu['lost_value']:.4f} ({100 * lu['lost_share']:.2f} %)" if lu else ""
            print(f"  value={r['value']:.6f} rel.err={r['relative_error']:.2e} within={r['within_tolerance']} "
                  f"wall={r['wall_s']:.3f}s{lost}")
    ok = (not out["modes"]["none"]["all_within_tolerance"]) and out["modes"]["lock"]["all_within_tolerance"] \
        and out["modes"]["local"]["all_within_tolerance"]
    out["ok"] = ok
    path = c.save("DPC/results/lab2/shared-sum", "summary.json", out)
    print(f"\n{'OK' if ok else 'CHECK'} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
