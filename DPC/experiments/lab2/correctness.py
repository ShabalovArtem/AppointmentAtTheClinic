"""О2.1: результати для 1, 2 і 4 процесів за однакових параметрів і допуску.

Для кожного методу (середні прямокутники, трапеції, Сімпсон) — n = 10^6, Q(t) на [0; 12], режим local.
Перевіряється: відносна похибка відносно еталона (первісна) ≤ допуску 10^-6, і результати різної
кількості процесів відрізняються між собою не більше ніж на допуск (порівняння не на точну рівність).

Із кореня проєкту:  python DPC/experiments/lab2/correctness.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402


def main() -> int:
    c.utf8_stdout()
    rows, ok = [], True
    for method in ("midpoint", "trapezoid", "simpson"):
        results = {p: c.compute(n=1_000_000, workers=p, method=method) for p in (1, 2, 4)}
        ref = results[1]["reference"]
        values = [r["value"] for r in results.values()]
        spread_rel = (max(values) - min(values)) / abs(ref)
        tol = results[1]["tolerance"]
        row = {"method": method, "reference": ref, "tolerance": tol, "spread_between_p_rel": spread_rel,
               "by_workers": {p: {"value": r["value"], "relative_error": r["relative_error"],
                                  "within_tolerance": r["within_tolerance"], "wall_s": r["wall_s"],
                                  "units_per_part": [x["units"] for x in r["timing"]["parts"]]}
                              for p, r in results.items()}}
        row_ok = all(r["within_tolerance"] for r in results.values()) and spread_rel <= tol
        ok &= row_ok
        rows.append(row)
        print(f"{method:10s} ref={ref:.10f}  " + "  ".join(
            f"p={p}: {r['value']:.12f} (rel {r['relative_error']:.1e})" for p, r in results.items())
            + f"  spread {spread_rel:.1e}  {'OK' if row_ok else 'FAIL'}")
    path = c.save("DPC/results/lab2/correctness", "summary.json", {"n": 1_000_000, "rows": rows, "ok": ok})
    print(f"{'OK' if ok else 'FAIL'} -> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
