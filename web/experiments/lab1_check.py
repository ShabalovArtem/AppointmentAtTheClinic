"""Перевірка ЛР1 Web (C02) через nginx — лише стандартна бібліотека Python.

Із кореня проєкту (стек запущено: docker compose up -d --build nginx api):
    python web/experiments/lab1_check.py --out web/results/lab1

Кожна перевірка друкує OK/FAIL; підсумок і всі відповіді — у summary.json.
Скрипт створює записи сайту; перед повторним запуском скиньте їх:
    docker compose exec -T postgres psql -U app -d app < web/experiments/reset_web.sql
"""

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

results: list[dict] = []


def call(base, method, path, user=None, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("X-User-Id", user)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, hdrs, payload = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        status, hdrs, payload = err.code, dict(err.headers), err.read()
    hdrs = {k.lower(): v for k, v in hdrs.items()}
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = payload.decode(errors="replace")
    return status, hdrs, parsed


def check(name, ok, evidence):
    results.append({"check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--app-a", default="http://localhost:8001")
    ap.add_argument("--app-b", default="http://localhost:8002")
    ap.add_argument("--out", default="web/results/lab1")
    a = ap.parse_args()
    B = a.base

    # О1.6: /health на кожному екземплярі, X-Request-ID, балансування на обидва екземпляри
    for name, url in (("app-a", a.app_a), ("app-b", a.app_b)):
        s, h, d = call(url, "GET", "/health")
        check(f"О1.6 /health {name}", s == 200 and d.get("instance") == name, {"status": s, "body": d})
    instances = Counter()
    for _ in range(20):
        s, h, _ = call(B, "GET", "/v1/doctors")
        instances[h.get("x-instance")] += 1
    s, h, _ = call(B, "GET", "/health", headers={"X-Request-ID": "check-req-1"})
    check("О1.6 X-Request-ID повертається", h.get("x-request-id") == "check-req-1", {"x-request-id": h.get("x-request-id")})
    check("О1.6 nginx розподіляє на app-a і app-b", instances["app-a"] > 0 and instances["app-b"] > 0, dict(instances))

    # каталог
    s, _, d = call(B, "GET", "/v1/doctors")
    check("каталог лікарів", s == 200 and len(d["items"]) >= 1, {"status": s, "count": len(d["items"])})
    s, _, d = call(B, "GET", "/v1/doctors/doc-1/days")
    check("дні лікаря з вільними слотами й ціною в копійках",
          s == 200 and all(isinstance(x["price"]["amount_minor"], int) for x in d["items"]), {"status": s, "first": d["items"][0]})
    s, _, d = call(B, "GET", "/v1/doctors/no-such/days")
    check("невідомий лікар — 404 Problem Details", s == 404 and d.get("status") == 404, {"status": s, "body": d})

    # О1.4: головна операція
    s, h, d = call(B, "POST", "/v1/bookings", "p-1002", {"doctor_day_id": "dd-main", "slots": 2, "visit_reason": "огляд"})
    created = d if s == 201 else {}
    check("О1.4 normal — 201 + Location, id рядок, held",
          s == 201 and h.get("location") == f"/v1/bookings/{d['id']}" and isinstance(d["id"], str) and d["status"] == "held",
          {"status": s, "location": h.get("location"), "body": d})
    check("О1.4 вартість = ціна слоту × слоти (цілі копійки)",
          created.get("price") == {"amount_minor": 100000, "currency": "UAH"}, created.get("price"))
    s, _, d = call(B, "GET", f"/v1/bookings/{created.get('id')}", "p-1002")
    check("запис видно власнику після 201 (зафіксовано до відповіді)", s == 200 and d["id"] == created.get("id"), {"status": s})
    s, _, d = call(B, "GET", f"/v1/bookings/{created.get('id')}", "p-1003")
    check("чужий запис — 404", s == 404, {"status": s})
    s, _, d = call(B, "POST", "/v1/bookings", "p-1002", {"doctor_day_id": "dd-main", "slots": 1})
    check("інваріант 2: другий активний запис до того ж лікаря на день — 409", s == 409, {"status": s, "body": d})
    s, _, d = call(B, "POST", "/v1/bookings", "p-1002", {"doctor_day_id": "no-such-day", "slots": 1})
    check("missing — 404", s == 404 and d.get("type", "").endswith("doctor-day-not-found"), {"status": s, "body": d})
    s, _, d = call(B, "POST", "/v1/bookings", "p-1002", {"doctor_day_id": "dd-main", "slots": 5})
    check("неправильні дані (slots=5) — 422", s == 422, {"status": s, "body": d})
    s, _, d = call(B, "POST", "/v1/bookings", "p-1002", {"doctor_day_id": "dd-main", "slots": 1, "status": "confirmed"})
    check("зайве поле (status) — 422", s == 422, {"status": s, "body": d})
    s, _, d = call(B, "POST", "/v1/bookings", "p-1002", raw=b'{"doctor_day_id": ')
    check("пошкоджений JSON — 400", s == 400 and d.get("status") == 400, {"status": s, "body": d})
    s, _, d = call(B, "POST", "/v1/bookings", None, {"doctor_day_id": "dd-main", "slots": 1})
    check("без X-User-Id — 401", s == 401, {"status": s, "body": d})

    # 409: останній слот dd-one, 20 одночасних пацієнтів через обидва екземпляри (попередньо до О2.1)
    statuses: list[int] = []
    users = [f"p-100{i % 5 + 1}" for i in range(5)]
    barrier = threading.Barrier(5)

    def race(u):
        barrier.wait()
        statuses.append(call(B, "POST", "/v1/bookings", u, {"doctor_day_id": "dd-one", "slots": 1})[0])

    threads = [threading.Thread(target=race, args=(u,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c = Counter(statuses)
    check("останній слот: не більше 1 успіху, решта 409", c[201] <= 1 and c[201] + c[409] == len(users), dict(c))

    # О1.5: список із курсором — усі сторінки без пропусків і дублів
    for i in range(5):  # p-1004 записується до кількох лікарів/днів
        day = ["dd-doc1-1202", "dd-doc1-1203", "dd-doc2-1202", "dd-doc3-1203", "dd-doc4-1202"][i]
        call(B, "POST", "/v1/bookings", "p-1004", {"doctor_day_id": day, "slots": 1})
    seen, cursor, pages = [], None, 0
    while True:
        s, _, d = call(B, "GET", "/v1/bookings?limit=2" + (f"&cursor={cursor}" if cursor else ""), "p-1004")
        pages += 1
        seen += [(x["created_at"], x["id"]) for x in d["items"]]
        cursor = d["next_cursor"]
        if not cursor or pages > 50:
            break
    check("О1.5 усі сторінки: без дублів, від найновіших",
          len(seen) == len(set(seen)) >= 5 and seen == sorted(seen, reverse=True), {"pages": pages, "items": len(seen)})
    s, _, d = call(B, "GET", "/v1/bookings", "p-1003")
    check("О1.5 лише власні записи", all(x["patient_id"] == "p-1003" for x in d["items"]), {"count": len(d["items"])})
    for q in ("limit=0", "limit=51", "cursor=not-a-cursor"):
        s, _, d = call(B, "GET", f"/v1/bookings?{q}", "p-1004")
        check(f"О1.5 {q} — {'400' if 'cursor' in q else '422'}", s == (400 if "cursor" in q else 422), {"status": s})

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK; results in {out / 'summary.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
