"""Спільний дослід Web + DPC: сайт і кол-центр одночасно займають слоти того самого дня лікаря.

dd-joint має 10 слотів. Одночасно: 5 пацієнтів сайту (POST /v1/bookings через nginx, Web) і
20 запитів кол-центру (POST /v1/booking-requests у чергу DPC, з них один — для пацієнта p-1001,
який також записується через сайт). Обидва канали викликають ту саму функцію бази take_slots(),
тому очікується: зайнято рівно 10 слотів, slots_taken = сума активних записів обох каналів,
і в p-1001 не більше одного активного запису на цей день.

Із кореня проєкту (стек запущено: docker compose up -d --build nginx api):
    docker compose exec -T postgres psql -U app -d app < experiments/joint/reset.sql
    python experiments/joint/both_channels.py --out experiments/joint/results
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

DAY = "dd-joint"


def call(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://localhost:8080")
    ap.add_argument("--dpc", default="http://localhost:8000")
    ap.add_argument("--token", default="dev-token")
    ap.add_argument("--call-center", type=int, default=20)
    ap.add_argument("--out", default="experiments/joint/results")
    a = ap.parse_args()
    auth = {"Authorization": f"Bearer {a.token}"}

    s, before = call(f"{a.dpc}/v1/doctor-day/{DAY}", headers=auth)
    if s != 200 or before["slots_taken"] != 0:
        print(f"{DAY} is not reset: {before}; run experiments/joint/reset.sql")
        return 2

    tag = uuid.uuid4().hex[:6]
    web_users = [f"p-100{i}" for i in range(1, 6)]
    cc_patients = ["p-1001"] + [f"cc-{tag}-{i:02d}" for i in range(1, a.call_center)]
    cc_ids = [f"joint-{tag}-{i:02d}" for i in range(a.call_center)]
    web_status: dict[str, int] = {}
    cc_status: dict[str, int] = {}
    barrier = threading.Barrier(len(web_users) + len(cc_ids))

    def web(user):
        barrier.wait()
        web_status[user] = call(f"{a.web}/v1/bookings", "POST", {"doctor_day_id": DAY, "slots": 1}, {"X-User-Id": user})[0]

    def cc(rid, patient):
        barrier.wait()
        body = {"request_id": rid, "doctor_day_id": DAY, "patient_id": patient, "slots": 1}
        cc_status[rid] = call(f"{a.dpc}/v1/booking-requests", "POST", body, auth)[0]

    threads = [threading.Thread(target=web, args=(u,)) for u in web_users]
    threads += [threading.Thread(target=cc, args=(r, p)) for r, p in zip(cc_ids, cc_patients)]
    started = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]

    finals = {}
    deadline = time.monotonic() + 60
    for rid in cc_ids:
        while time.monotonic() < deadline:
            s, d = call(f"{a.dpc}/v1/booking-requests/{rid}", headers=auth)
            if d and d.get("state") in {"done", "rejected", "failed"}:
                finals[rid] = d
                break
            time.sleep(0.2)
    s, after = call(f"{a.dpc}/v1/doctor-day/{DAY}", headers=auth)
    p1001_web = web_status.get("p-1001")
    p1001_cc = finals.get(cc_ids[0], {})

    summary = {
        "doctor_day": DAY,
        "slots_total": after["slots_total"],
        "web_requests": len(web_users),
        "web_statuses": dict(Counter(web_status.values())),
        "call_center_requests": len(cc_ids),
        "call_center_submit_statuses": dict(Counter(cc_status.values())),
        "call_center_final": dict(Counter(f"{d['state']}:{d['reason'] or ''}" for d in finals.values())),
        "p-1001": {"web": p1001_web, "call_center": f"{p1001_cc.get('state')}:{p1001_cc.get('reason')}"},
        "seconds": round(time.monotonic() - started, 2),
        "resource_after": after,
    }
    ok = (
        after["invariant_ok"]
        and after["slots_taken"] == after["slots_total"]
        and not (p1001_web == 201 and p1001_cc.get("state") == "done")
    )
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
