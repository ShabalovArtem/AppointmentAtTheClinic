"""О2.2 (точність), Д2.2 (екстраполяція Річардсона), Д2.4 (послідовне, попарне, компенсоване сумування).

A. Функція картки Q(t) на [0; 12], середні прямокутники й трапеції: n = 8 … 2048 (подвоєння). Тут похибка методу
   (~10^-1 … 10^-6) значно більша за похибку округлення (~10^-13), тож відношення e(n)/e(2n) показує порядок: ≈ 4.
   Річардсон: R = (4·I(2n) − I(n)) / 3.
B. Сімпсон на Q(t): похибка ≈ округлення для будь-якого n — метод точний для поліномів до 3-го степеня
   (Q кубічна, Q'''' = 0). Для порядку збіжності потрібна інша тестова функція:
C. Сімпсон на sin(t), [0; π]: n = 4 … 256, відношення ≈ 16; Річардсон з p = 4.
D. Д2.4: Сімпсон на Q(t) (похибка методу 0, лишається лише округлення), n = 10^6, 2·10^6, 4·10^6, 1 процес:
   порівняння order = naive / pairwise / kahan.

Із кореня проєкту:  python DPC/experiments/lab2/accuracy.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402


def series(method, function, ns, **extra):
    rows, prev = [], None
    for n in ns:
        r = c.compute(n=n, workers=1, method=method, function=function, richardson=True, **extra)
        err = abs(r["error"])
        row = {"n": n, "value": r["value"], "error": r["error"], "method_bound": r["method_error_bound"],
               "rounding_bound": r["rounding_error_bound"], "ratio_prev_to_this": (prev / err) if prev and err else None,
               "richardson_error": r["richardson"]["error"]}
        rows.append(row)
        prev = err
    return rows


def show(title, rows):
    print(f"\n{title}")
    print(f"{'n':>8} {'|error|':>12} {'bound':>12} {'e(n/2)/e(n)':>12} {'Richardson err':>15}")
    for r in rows:
        ratio = f"{r['ratio_prev_to_this']:.3f}" if r["ratio_prev_to_this"] else "-"
        print(f"{r['n']:>8} {abs(r['error']):>12.3e} {r['method_bound']:>12.3e} {ratio:>12} {abs(r['richardson_error']):>15.3e}")


def main() -> int:
    c.utf8_stdout()
    ns = [8 * 2 ** k for k in range(9)]  # 8 … 2048
    out = {
        "A_midpoint_queue": series("midpoint", "queue", ns),
        "A_trapezoid_queue": series("trapezoid", "queue", ns),
        "B_simpson_queue": series("simpson", "queue", [8, 64, 512, 4096]),
        "C_simpson_sin": series("simpson", "sin", [4 * 2 ** k for k in range(7)], a=0.0, b=math.pi),
    }
    show("A. midpoint, Q(t) [0;12]", out["A_midpoint_queue"])
    show("A. trapezoid, Q(t) [0;12]", out["A_trapezoid_queue"])
    show("B. simpson, Q(t) [0;12] — точний для кубічної функції", out["B_simpson_queue"])
    show("C. simpson, sin(t) [0;pi]", out["C_simpson_sin"])

    summ = []
    for n in (2_000_000, 4_000_000, 8_000_000, 16_000_000):
        row = {"n": n}
        # naive — ОДИН послідовний ланцюжок (порція = весь діапазон); pairwise — блоки по 64 + попарне
        # об'єднання блоків; kahan — компенсоване сумування
        for order, chunk in (("naive", n // 2), ("pairwise", 64), ("kahan", n // 2)):  # у Сімпсона n/2 одиниць
            r = c.compute(n=n, workers=1, method="simpson", order=order, chunk=chunk)
            row[order] = {"error": r["error"], "relative_error": r["relative_error"], "wall_s": r["wall_s"]}
        summ.append(row)
    out["D_summation"] = summ
    print("\nD. Д2.4 summation (simpson on Q, method error 0): |relative error|")
    print(f"{'n':>9} {'naive':>11} {'pairwise':>11} {'kahan':>11}")
    for row in summ:
        print(f"{row['n']:>9} " + " ".join(f"{row[o]['relative_error']:>11.2e}" for o in ("naive", "pairwise", "kahan")))
    path = c.save("DPC/results/lab2/accuracy", "summary.json", out)
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
