"""О3.4 (і О3.1): відмова після запису ефекту — after_effect_before_ack.

A. Один запит. Вмикається відмова (файл /faults/after_effect_before_ack у спільному томі; його бачать вузли Ray).
   Задача Ray перевіряє поліс, публікує ефект приймачу (ефект записано), і процес-виконавець гине до повернення
   результату. Ray повторює задачу (max_retries); повтор із тим самим ключем request_id отримує збережене рішення.
   Перевірка: стан done, у таблиці effect — deliveries = 2, записів booking із цим request_id — 1,
   slots_taken зріс на slots один раз, у журналі вузла — fault.hit, інваріант ЛР1 виконано.
B. Інваріант під час відмови: день race-locked (20 слотів) скинуто, відмову ввімкнено, 30 одночасних запитів —
   рівно 20 done, 10 rejected/no-free-slots, invariant_ok, а один ефект доставлено двічі.

Із кореня проєкту (стек запущено, EXECUTION=ray):  python DPC/experiments/lab3/fault.py
"""

import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab3common as c  # noqa: E402


def submit(rid, day, patient, slots=1):
    return c.call("POST", "/v1/booking-requests", {"request_id": rid, "doctor_day_id": day, "patient_id": patient,
                                                   "slots": slots})[0]


def main() -> int:
    c.utf8_stdout()
    out = {}
    # --- A
    day = "dd-main"
    before = c.resource(day)
    rid = f"fault-{uuid.uuid4().hex[:10]}"
    c.compose("exec", "-T", "api", "touch", "/faults/after_effect_before_ack")
    s = submit(rid, day, f"cc-{rid}", 2)
    final = c.wait_final(rid)
    # стан done записує приймач уже під час ПЕРШОЇ доставки (до загибелі виконавця); повтор задачі Ray
    # відбувається після цього — чекаємо, доки диспетчер отримає результат повтору (ray.task_done)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        logs = c.compose("logs", "api", "--since", "3m", check=False)
        if any(rid in line and "ray.task_" in line for line in logs.splitlines()):
            break
        time.sleep(1)
    after = c.resource(day)
    effect = c.psql(f"SELECT decision || '|' || deliveries || '|' || coalesce(first_worker,'') || '|' || coalesce(last_worker,'') "
                    f"FROM effect WHERE effect_key = '{rid}'")
    bookings = int(c.psql(f"SELECT count(*) FROM booking WHERE request_id = '{rid}'"))
    hits = c.fault_hits()
    decision, deliveries, first_w, last_w = effect.split("|")
    out["A_single"] = {"request_id": rid, "submit": s, "final_state": final and final["state"], "effect_decision": decision,
                       "deliveries": int(deliveries), "first_worker": first_w, "last_worker": last_w,
                       "bookings_with_request_id": bookings, "slots_taken_delta": after["slots_taken"] - before["slots_taken"],
                       "invariant_ok": after["invariant_ok"], "fault_hit": hits[-1] if hits else None}
    ok_a = (final and final["state"] == "done" and int(deliveries) == 2 and bookings == 1
            and after["slots_taken"] - before["slots_taken"] == 2 and after["invariant_ok"] and bool(hits))
    print("A:", {k: v for k, v in out["A_single"].items() if k != "fault_hit"})
    print("   fault.hit:", (hits[-1][:160] if hits else None), "->", "OK" if ok_a else "FAIL")

    # --- B
    day = "race-locked"
    c.psql(f"""BEGIN; DELETE FROM effect WHERE doctor_day_id = '{day}'; DELETE FROM booking WHERE doctor_day_id = '{day}';
               DELETE FROM booking_request WHERE doctor_day_id = '{day}'; UPDATE doctor_day SET slots_taken = 0 WHERE id = '{day}'; COMMIT;""")
    tag = uuid.uuid4().hex[:6]
    ids = [f"fb-{tag}-{i:02d}" for i in range(30)]
    c.compose("exec", "-T", "api", "touch", "/faults/after_effect_before_ack")
    barrier = threading.Barrier(len(ids))
    statuses = []

    def go(i):
        barrier.wait()
        statuses.append(submit(ids[i], day, f"cc-{tag}-{i:02d}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(ids))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    finals = [c.wait_final(r) for r in ids]
    deadline = time.monotonic() + 90  # дочекатися завершення всіх задач (зокрема повтору після відмови)
    while time.monotonic() < deadline and int(c.psql("SELECT count(*) FROM booking_request br WHERE br.doctor_day_id = "
                                                      f"'{day}' AND br.id LIKE 'fb-{tag}-%'")) and             c.call("GET", "/metrics")[1].get("ray_in_flight", 0) > 0:
        time.sleep(1)
    res = c.resource(day)
    dup = c.psql(f"SELECT count(*) FILTER (WHERE deliveries > 1) || '|' || coalesce(max(deliveries), 0) "
                 f"FROM effect WHERE doctor_day_id = '{day}'").split("|")
    out["B_invariant"] = {"requests": len(ids), "submit": dict(Counter(statuses)),
                          "final": dict(Counter(f"{f['state']}:{f['reason'] or ''}" for f in finals if f)),
                          "effects_delivered_more_than_once": int(dup[0]), "max_deliveries": int(dup[1]),
                          "resource_after": {k: res[k] for k in ("slots_total", "slots_taken", "booked", "invariant_ok")}}
    ok_b = res["invariant_ok"] and res["booked"] == 20 and int(dup[0]) >= 1
    print("B:", out["B_invariant"], "->", "OK" if ok_b else "FAIL")
    out["ok"] = bool(ok_a and ok_b)
    path = c.save("DPC/results/lab3/fault", "summary.json", out)
    print(f"{'OK' if out['ok'] else 'FAIL'} -> {path}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
