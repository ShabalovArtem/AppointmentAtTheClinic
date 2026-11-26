"""О3.3: атомарне приймання ефекту — повторні й одночасні доставки того самого ключа.

Доставки надсилаються напряму приймачу POST /internal/v1/effects (як це робить задача Ray):
  1. 12 одночасних доставок одного ефекту (ключ K, 2 слоти) — один запис, slots_taken +2, deliveries = 12,
     усі відповіді з тим самим рішенням, рівно одна з duplicate = false;
  2. повторна послідовна доставка K — duplicate = true, ресурс не змінюється;
  3. ефект із недійсним полісом — rejected/policy-invalid, ресурс не змінюється, повтор повертає те саме.

Із кореня проєкту:  python DPC/experiments/lab3/receiver_check.py
"""

import sys
import threading
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab3common as c  # noqa: E402

results: list[dict] = []


def check(name, ok, evidence):
    results.append({"check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def deliver(key, day, patient, slots, policy_ok=True, worker="check"):
    return c.call("POST", "/internal/v1/effects", {"effect_key": key, "doctor_day_id": day, "patient_id": patient,
                                                    "slots": slots, "policy_ok": policy_ok, "worker": worker})


def main() -> int:
    c.utf8_stdout()
    day = "dd-main"
    key = f"eff-{uuid.uuid4().hex[:10]}"
    before = c.resource(day)
    out: list = []
    barrier = threading.Barrier(12)

    def go(i):
        barrier.wait()
        out.append(deliver(key, day, f"cc-{key}", 2, worker=f"w{i}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    mid = c.resource(day)
    row = c.psql(f"SELECT deliveries || '|' || decision FROM effect WHERE effect_key = '{key}'").split("|")
    bookings = int(c.psql(f"SELECT count(*) FROM booking WHERE request_id = '{key}'"))
    decisions = Counter(d["decision"] for s, d in out)
    firsts = sum(1 for s, d in out if not d["duplicate"])
    check("12 одночасних доставок: один запис, +2 слоти, deliveries = 12, одна перша доставка",
          bookings == 1 and mid["slots_taken"] - before["slots_taken"] == 2 and int(row[0]) == 12 and firsts == 1
          and decisions == Counter({"done": 12}) and mid["invariant_ok"],
          {"statuses": dict(Counter(s for s, _ in out)), "decisions": dict(decisions), "first_deliveries": firsts,
           "deliveries": int(row[0]), "bookings": bookings, "slots_taken_delta": mid["slots_taken"] - before["slots_taken"]})

    s, d = deliver(key, day, f"cc-{key}", 2, worker="late")
    after = c.resource(day)
    check("повторна доставка — збережене рішення, ресурс без змін",
          d["duplicate"] and d["decision"] == "done" and d["deliveries"] == 13 and after["slots_taken"] == mid["slots_taken"],
          {"reply": d, "slots_taken": after["slots_taken"]})

    key2 = f"eff-{uuid.uuid4().hex[:10]}"
    s1, d1 = deliver(key2, day, f"cc-{key2}", 1, policy_ok=False)
    s2, d2 = deliver(key2, day, f"cc-{key2}", 1, policy_ok=False)
    after2 = c.resource(day)
    check("недійсний поліс — rejected/policy-invalid; повтор — те саме рішення, ресурс без змін",
          d1["decision"] == "rejected" and d1["reason"] == "policy-invalid" and d2["duplicate"] and d2["reason"] == "policy-invalid"
          and after2["slots_taken"] == after["slots_taken"], {"first": d1, "repeat": d2})

    path = c.save("DPC/results/lab3/receiver", "summary.json", results)
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK -> {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
