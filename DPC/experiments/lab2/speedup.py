"""О2.3: прискорення для 1, 2 і 4 процесів.

Однакова задача: Q(t) на [0; 12], середні прямокутники, n = 2·10^7, режим local, порція 10 000.
Перед вимірами — прогрівальний запуск (не враховується). Для кожного p — REPEATS вимірів;
медіана, розкид (max − min), Sp = T1/Tp, Ep = Sp/p за медіанами. Tp — повний час розрахунку
в сервісі (`wall_s`: створення процесів, запуск, обчислення, передавання результатів, об'єднання).

Із кореня проєкту:
    python DPC/experiments/lab2/speedup.py                          # базовий режим (start_method з конфігурації)
    python DPC/experiments/lab2/speedup.py --affinity spread --out DPC/results/lab2/speedup-spread
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

PARTS = ("spawn_s", "start_s", "compute_s", "collect_merge_s")


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20_000_000)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--workers", default="1,2,4")
    ap.add_argument("--start-method", default=None)
    ap.add_argument("--chunk", type=int, default=10_000)
    ap.add_argument("--affinity", default="none", choices=["none", "spread"])
    ap.add_argument("--out", default="DPC/results/lab2/speedup")
    a = ap.parse_args()
    base = {"n": a.n, "method": "midpoint", "mode": "local", "chunk": a.chunk, "affinity": a.affinity}
    if a.start_method:
        base["start_method"] = a.start_method

    c.compute(**base, workers=4)  # прогрів: forkserver, кеші, частота процесора
    rows = {}
    for p in [int(x) for x in a.workers.split(",")]:
        runs = [c.compute(**base, workers=p) for _ in range(a.repeats)]
        walls = [r["timing"]["wall_s"] for r in runs]
        rows[p] = {
            "wall": c.stats(walls),
            "components_median": {k: statistics.median(r["timing"][k] for r in runs) for k in PARTS},
            "max_relative_error": max(r["relative_error"] for r in runs),
            "start_method": runs[0]["params"]["start_method"],
        }
    t1 = rows[1]["wall"]["median"] if 1 in rows else None
    for p, row in rows.items():
        if t1:
            row["Sp"] = t1 / row["wall"]["median"]
            row["Ep"] = row["Sp"] / p
    summary = {"task": base, "repeats": a.repeats, "rows": rows}
    path = c.save(a.out, "summary.json", summary)
    print(f"start_method={rows[next(iter(rows))]['start_method']} affinity={a.affinity} n={a.n}")
    print(f"{'p':>2} {'median, s':>10} {'min':>8} {'max':>8} {'Sp':>6} {'Ep':>6}  start/compute/collect, s")
    for p, row in rows.items():
        w, comp = row["wall"], row["components_median"]
        print(f"{p:>2} {w['median']:>10.3f} {w['min']:>8.3f} {w['max']:>8.3f} {row.get('Sp', 0):>6.2f} {row.get('Ep', 0):>6.2f}"
              f"  {comp['start_s']:.3f}/{comp['compute_s']:.3f}/{comp['collect_merge_s']:.3f}  rel.err {row['max_relative_error']:.1e}")
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
