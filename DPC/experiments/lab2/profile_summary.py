"""Зведення профілю py-spy (формат raw / collapsed stacks) за функціями й процесами.

    python DPC/experiments/lab2/profile_summary.py profile.txt
Рядок raw: "<кадри через ;> <кількість вибірок>". Скрипт рахує частку вибірок, у стеку яких є функція
(inclusive), окремо для процесів-частин (run_part) і для батьківського процесу.
"""

import re
import sys
from collections import Counter

FUNCS = ["run_part", "portion_sum", "<lambda>", "_run", "get (multiprocessing/queues.py", "start (multiprocessing",
         "_bootstrap", "_launch", "import", "histogram_part", "late_minutes"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lines = [l for l in open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
    total = Counter()
    inclusive = Counter()
    leaf = Counter()
    for line in lines:
        m = re.match(r"^(.*) (\d+)$", line)
        if not m:
            continue
        stack, n = m.group(1), int(m.group(2))
        proc = "part" if "run_part" in stack else ("parent" if "uvicorn" in stack.split(";")[0] else "other")
        total[proc] += n
        frames = stack.split(";")
        last = frames[-1]
        leaf[(proc, re.sub(r"\s*\(.*", "", last.strip()))] += n
        for f in FUNCS:
            if f in stack:
                inclusive[(proc, f)] += n
    print("samples by process kind:", dict(total))
    for proc in ("part", "parent", "other"):
        if not total[proc]:
            continue
        print(f"\n== {proc} ({total[proc]} samples)")
        for (p, f), n in sorted(inclusive.items(), key=lambda x: -x[1]):
            if p == proc:
                print(f"  inclusive {f:40s} {n:6d}  {100 * n / total[proc]:5.1f} %")
        for (p, f), n in leaf.most_common():
            if p == proc and n / total[proc] > 0.02:
                print(f"  leaf      {f:40s} {n:6d}  {100 * n / total[proc]:5.1f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main())
