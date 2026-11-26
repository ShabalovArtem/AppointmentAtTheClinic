"""Обчислювальні частини ЛР2 (DPC), що виконуються в окремих процесах.

Модуль навмисно легкий (лише math і time): процес-виконавець імпортує тільки його, а не FastAPI,
базу чи Ray. Функції працюють із «одиницями» сітки:
  midpoint  — одиниця = відрізок k: h·f(a + (k + ½)h);                   одиниць n
  trapezoid — одиниця = відрізок k: h/2·(f(x_k) + f(x_{k+1}));            одиниць n
  simpson   — одиниця = пара відрізків j: h/3·(f(x_2j) + 4f(x_2j+1) + f(x_2j+2)); одиниць n/2
Проміжок [0, units) ділиться на `workers` суцільних діапазонів без пропусків і перекриття.
Кожна частина рахує свій діапазон порціями по `chunk` одиниць: після кожної порції вона додає порцію
до спільної суми (режими none/lock) або до локальної суми (local) і перевіряє сигнал скасування.
"""

import math
import os
import time

EPS = 2.0 ** -53  # одиничне округлення double


# --------------------------------------------------------------- функції


def make_f(function: str, amp: float, T: float):
    if function == "queue":  # картка C02: Q(t) = amp·t²·(T − t) — людей у черзі реєстратури
        return lambda t: amp * t * t * (T - t)
    if function == "sin":  # тестова функція для порядку збіжності (Сімпсон точний для кубічної Q)
        return math.sin
    if function == "exp":
        return math.exp
    raise ValueError(function)


def antiderivative(function: str, amp: float, T: float):
    if function == "queue":
        return lambda t: amp * (T * t ** 3 / 3 - t ** 4 / 4)
    if function == "sin":
        return lambda t: -math.cos(t)
    if function == "exp":
        return math.exp
    raise ValueError(function)


def derivative_bound(function: str, amp: float, T: float, a: float, b: float, order: int) -> float:
    """max |f^(order)| на [a, b] — для оцінки похибки методу."""
    if function == "queue":
        if order == 2:  # Q'' = amp·(2T − 6t), лінійна: максимум модуля на кінцях
            return abs(amp) * max(abs(2 * T - 6 * a), abs(2 * T - 6 * b))
        return 0.0  # Q'''' = 0: Сімпсон інтегрує кубічний поліном точно
    if function == "sin":
        return 1.0
    if function == "exp":
        return math.exp(max(a, b))
    raise ValueError(function)


def units(method: str, n: int) -> int:
    return n // 2 if method == "simpson" else n


def split(total_units: int, workers: int) -> list[tuple[int, int]]:
    """Суцільні діапазони [lo, hi), що разом покривають [0, total_units) без пропусків і перекриття."""
    return [(k * total_units // workers, (k + 1) * total_units // workers) for k in range(workers)]


# --------------------------------------------------------------- суми порцій


def portion_sum(method: str, f, a: float, h: float, i: int, j: int, kahan: bool) -> float:
    """Сума одиниць [i, j) (без множника h), послідовно або з компенсацією Кехена."""
    s = 0.0
    c = 0.0
    if method == "midpoint":
        if kahan:
            for k in range(i, j):
                y = f(a + (k + 0.5) * h) - c
                t = s + y
                c = (t - s) - y
                s = t
        else:
            for k in range(i, j):
                s += f(a + (k + 0.5) * h)
        return s
    if method == "trapezoid":
        prev = f(a + i * h)
        if kahan:
            for k in range(i, j):
                cur = f(a + (k + 1) * h)
                y = 0.5 * (prev + cur) - c
                t = s + y
                c = (t - s) - y
                s = t
                prev = cur
        else:
            for k in range(i, j):
                cur = f(a + (k + 1) * h)
                s += 0.5 * (prev + cur)
                prev = cur
        return s
    # simpson: одиниця j — точки 2j, 2j+1, 2j+2; спільні кінці пар рахуються в кожній парі
    prev = f(a + 2 * i * h)
    if kahan:
        for k in range(i, j):
            mid = f(a + (2 * k + 1) * h)
            cur = f(a + (2 * k + 2) * h)
            y = (prev + 4.0 * mid + cur) / 3.0 - c
            t = s + y
            c = (t - s) - y
            s = t
            prev = cur
    else:
        for k in range(i, j):
            mid = f(a + (2 * k + 1) * h)
            cur = f(a + (2 * k + 2) * h)
            s += (prev + 4.0 * mid + cur) / 3.0
            prev = cur
    return s


def pairwise(values: list[float]) -> float:
    """Попарне (каскадне) сумування: похибка росте як O(log m), а не O(m)."""
    while len(values) > 1:
        values = [values[i] + values[i + 1] if i + 1 < len(values) else values[i] for i in range(0, len(values), 2)]
    return values[0] if values else 0.0


def kahan_sum(values: list[float]) -> float:
    s = 0.0
    c = 0.0
    for v in values:
        y = v - c
        t = s + y
        c = (t - s) - y
        s = t
    return s


# --------------------------------------------------------------- частина (процес-виконавець)


def run_part(params: dict, k: int, lo: int, hi: int, out, shared, active, cancel) -> None:
    """Тіло процесу: рахує одиниці [lo, hi) порціями й повідомляє результат у черзу `out`.

    mode = local — лише локальна сума, батьківський процес об'єднує результати частин;
    mode = lock  — кожна порція додається до спільного Value під його блокуванням;
    mode = none  — кожна порція додається до спільного RawValue без синхронізації:
                   `shared.value += x` — це «прочитати, додати, записати», тож оновлення губляться.
    """
    if params.get("affinity") == "spread":
        # О2.3, гіпотеза: частина k — на логічний процесор 2k, тобто кожна на окремому фізичному ядрі
        # (у VM Docker логічні процесори 2i і 2i+1 — два потоки одного ядра)
        cpus = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, {cpus[(2 * k) % len(cpus)]})
    t_ready = time.perf_counter()
    f = make_f(params["function"], params["amp"], params["T"])
    method, a, h = params["method"], params["a"], params["h"]
    mode, order, chunk = params["mode"], params["order"], params["chunk"]
    kahan = order == "kahan"
    sums: list[float] = []  # суми порцій (local/pairwise/kahan об'єднуються в батьківському процесі)
    local = 0.0
    contributed = 0.0  # скільки частина ДОДАЛА до спільної суми (для підрахунку втрачених оновлень)
    portions = 0
    cancelled = False
    i = lo
    while i < hi:
        j = min(i + chunk, hi)
        ps = portion_sum(method, f, a, h, i, j, kahan)
        portions += 1
        if mode == "local":
            if order == "naive":
                local += ps
            else:
                sums.append(ps)
        elif mode == "lock":
            with shared.get_lock():
                shared.value += ps
            contributed += ps
        else:  # none
            shared.value += ps
            contributed += ps
        i = j
        if cancel.is_set():  # сигнал скасування перевіряється між порціями
            cancelled = True
            break
    t_done = time.perf_counter()
    with active.get_lock():
        active.value -= 1
    out.put({"k": k, "lo": lo, "hi": hi, "local": local, "sums": sums, "contributed": contributed,
             "portions": portions, "cancelled": cancelled, "t_ready": t_ready, "t_done": t_done})


# --------------------------------------------------------------- гістограма (Д2.5)

_MASK = (1 << 64) - 1
LATE_BINS = ["0"] + [f"{lo}-{lo + 4}" for lo in range(1, 60, 5)] + ["61+"]


def _splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK
    return x ^ (x >> 31)


def late_minutes(seed: int, i: int) -> int:
    """Запізнення пацієнта i (хв). Залежить лише від seed і ГЛОБАЛЬНОГО номера пацієнта, а не від
    номера процесу, тому результат не залежить від кількості процесів. 55 % приходять вчасно,
    решта запізнюються з експоненційним розподілом (середнє 12 хв)."""
    r1 = _splitmix64((seed << 32) ^ (2 * i))
    if r1 < int(0.55 * 2 ** 64):
        return 0
    u = (_splitmix64((seed << 32) ^ (2 * i + 1)) + 1) / 2.0 ** 64
    return max(1, math.ceil(-math.log(u) * 12.0))


def late_bin(m: int) -> str:
    if m <= 0:
        return "0"
    if m > 60:
        return "61+"
    lo = (m - 1) // 5 * 5 + 1
    return f"{lo}-{lo + 4}"


def histogram_part(seed: int, lo: int, hi: int, out) -> None:
    t_ready = time.perf_counter()
    local: dict[str, int] = {}  # локальний словник частини; об'єднання — наприкінці в батьківському процесі
    for i in range(lo, hi):
        b = late_bin(late_minutes(seed, i))
        local[b] = local.get(b, 0) + 1
    out.put({"lo": lo, "hi": hi, "counts": local, "t_ready": t_ready, "t_done": time.perf_counter()})
