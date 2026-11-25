"""Перевірка решти маршрутів картки C02 (розд. 3) і входу — лише стандартна бібліотека Python.

Із кореня проєкту (стек запущено):
    python web/experiments/card_routes_check.py --out web/results/card-routes

Що перевіряється: реєстрація пацієнта, вхід/вихід, /v1/me, доступ лише до своїх записів;
PATCH причини візиту з If-Match (200, 412, 428, 422); скасування з поверненням слотів і повторне
скасування без другого повернення; закриття дня реєстратором (пацієнт — 403), скасування активних
записів, 409 для нових записів сайту й відмова day-closed у кол-центрі DPC.
Скрипт закриває день dd-doc4-1203; скидання: docker compose exec -T postgres psql -U app -d app < web/experiments/reset_web.sql
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

results: list[dict] = []


def call(base, method, path, token=None, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if method == "POST" and path == "/v1/bookings" and "Idempotency-Key" not in (headers or {}):
        req.add_header("Idempotency-Key", f"chk-{uuid.uuid4()}")  # з ЛР2 ключ обов'язковий
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    return status, {k.lower(): v for k, v in hdrs.items()}, (json.loads(raw) if raw else None)


def check(name, ok, evidence):
    results.append({"check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--dpc", default="http://localhost:8000")
    ap.add_argument("--out", default="web/results/card-routes")
    a = ap.parse_args()
    B = a.base

    # --- реєстрація, вхід, вихід
    login = f"patient-{uuid.uuid4().hex[:6]}"
    s, h, d = call(B, "POST", "/v1/auth/register", body={"login": login, "password": "secret-pass-1", "name": "Тест Пацієнт"})
    check("реєстрація пацієнта — 201, токен, роль patient", s == 201 and d["user"]["role"] == "patient" and d["access_token"],
          {"status": s, "user": d.get("user") if d else d})
    new_token = d["access_token"] if s == 201 else None
    s, _, d = call(B, "POST", "/v1/auth/register", body={"login": login, "password": "secret-pass-1", "name": "Двійник"})
    check("той самий логін — 409", s == 409, {"status": s})
    s, _, d = call(B, "POST", "/v1/auth/register", body={"login": "ab", "password": "short", "name": "X"})
    check("неправильні дані реєстрації — 422", s == 422, {"status": s, "errors": d.get("errors") if d else None})
    s, _, d = call(B, "GET", "/v1/me", new_token)
    check("GET /v1/me з токеном", s == 200 and d["login"] == login, {"status": s, "body": d})
    s, _, _ = call(B, "POST", "/v1/auth/login", body={"login": login, "password": "wrong-pass"})
    check("хибний пароль — 401", s == 401, {"status": s})
    s, _, d = call(B, "POST", "/v1/auth/login", body={"login": login.upper(), "password": "secret-pass-1"})
    check("вхід (логін без урахування регістру) — 200", s == 200 and d["user"]["login"] == login, {"status": s})
    second_token = d["access_token"]
    s, _, _ = call(B, "POST", "/v1/auth/logout", second_token)
    s2, _, _ = call(B, "GET", "/v1/me", second_token)
    check("вихід — 204, токен більше не діє (на будь-якому екземплярі)", s == 204 and s2 == 401, {"logout": s, "me_after": s2})
    s, _, _ = call(B, "GET", "/v1/bookings", "not-a-token")
    check("неправильний токен — 401", s == 401, {"status": s})

    # --- записи нового пацієнта: створення, If-Match, скасування
    s, h, d = call(B, "POST", "/v1/bookings", new_token, {"doctor_day_id": "dd-doc4-1202", "slots": 2})
    check("новий пацієнт записується — 201", s == 201, {"status": s})
    bid, etag = d["id"], h.get("etag")
    _, _, staff_login = call(B, "POST", "/v1/auth/login", body={"login": "registrar", "password": "registrar-pass"})
    staff = staff_login["access_token"]
    _, _, p2 = call(B, "POST", "/v1/auth/login", body={"login": "patient2", "password": "patient-pass"})
    s, _, _ = call(B, "GET", f"/v1/bookings/{bid}", p2["access_token"])
    check("інший пацієнт не бачить запису — 404", s == 404, {"status": s})

    s, _, _ = call(B, "PATCH", f"/v1/bookings/{bid}", new_token, {"visit_reason": "Головний біль"})
    check("PATCH без If-Match — 428", s == 428, {"status": s})
    s, h2, d = call(B, "PATCH", f"/v1/bookings/{bid}", new_token, {"visit_reason": "Головний біль"}, {"If-Match": etag})
    check("PATCH з поточним ETag — 200, нова версія", s == 200 and d["visit_reason"] == "Головний біль" and h2.get("etag") != etag,
          {"status": s, "etag_before": etag, "etag_after": h2.get("etag")})
    s, h3, _ = call(B, "PATCH", f"/v1/bookings/{bid}", new_token, {"visit_reason": "Інше"}, {"If-Match": etag})
    check("PATCH зі старим ETag — 412 (зміна не загубилась)", s == 412 and h3.get("etag") == h2.get("etag"), {"status": s})
    s, _, _ = call(B, "PATCH", f"/v1/bookings/{bid}", new_token, {"status": "confirmed"}, {"If-Match": h2.get("etag")})
    check("PATCH стану — 422", s == 422, {"status": s})

    def free(day):
        _, _, days = call(B, "GET", "/v1/doctors/doc-4/days")
        return next(x["slots_free"] for x in days["items"] if x["id"] == day)

    before = free("dd-doc4-1202")
    s, _, d = call(B, "POST", f"/v1/bookings/{bid}/cancel", new_token)
    after1 = free("dd-doc4-1202")
    s2, _, d2 = call(B, "POST", f"/v1/bookings/{bid}/cancel", new_token)
    after2 = free("dd-doc4-1202")
    check("скасування повертає 2 слоти; повтор — той самий стан, слоти вдруге не повертаються",
          s == 200 and d["status"] == "cancelled" and s2 == 200 and d2["status"] == "cancelled"
          and after1 == before + 2 and after2 == after1,
          {"free_before": before, "after_cancel": after1, "after_repeat": after2, "cancel_reason": d.get("cancel_reason")})
    s, _, _ = call(B, "PATCH", f"/v1/bookings/{bid}", new_token, {"visit_reason": "x"}, {"If-Match": d2["version"]})
    check("скасований запис не змінюється — 409", s == 409, {"status": s})

    # --- закриття дня реєстратором
    day = "dd-doc4-1203"
    s, _, active = call(B, "POST", "/v1/bookings", new_token, {"doctor_day_id": day, "slots": 1})
    s, _, _ = call(B, "POST", f"/v1/doctor-days/{day}/close", new_token)
    check("пацієнт не може закрити день — 403", s == 403, {"status": s})
    s, _, d = call(B, "POST", f"/v1/doctor-days/{day}/close", staff)
    check("реєстратор закриває день — 200, активний запис скасовано",
          s == 200 and d["closed"] and active["id"] in d["cancelled_bookings"], {"status": s, "body": d})
    s, _, b = call(B, "GET", f"/v1/bookings/{active['id']}", new_token)
    check("пацієнт бачить скасування з причиною day-closed", b["status"] == "cancelled" and b["cancel_reason"] == "day-closed", b)
    s, _, d = call(B, "POST", "/v1/bookings", new_token, {"doctor_day_id": day, "slots": 1})
    check("новий запис сайту на закритий день — 409", s == 409 and d["type"].endswith("day-closed"), {"status": s, "body": d})
    rid = f"closed-{uuid.uuid4().hex[:8]}"
    auth = {"Authorization": "Bearer dev-token"}
    call(a.dpc, "POST", "/v1/booking-requests", body={"request_id": rid, "doctor_day_id": day, "patient_id": "p-1003", "slots": 1}, headers=auth)
    final = None
    for _ in range(40):
        _, _, final = call(a.dpc, "GET", f"/v1/booking-requests/{rid}", headers=auth)
        if final and final["state"] in ("done", "rejected", "failed"):
            break
        time.sleep(0.25)
    check("кол-центр DPC на закритий день — rejected/day-closed (одне правило)",
          final and final["state"] == "rejected" and final["reason"] == "day-closed", final)
    s, _, d = call(B, "GET", f"/v1/doctors/doc-4/days")
    check("закритий день зник із каталогу сайту", all(x["id"] != day for x in d["items"]), {"days": [x["id"] for x in d["items"]]})
    s, _, d = call(B, "GET", "/v1/doctor-days", staff)
    check("розклад реєстратури показує закритий день", any(x["id"] == day and x["closed"] for x in d["items"]), {"status": s})
    s, _, _ = call(B, "GET", "/v1/doctor-days", new_token)
    check("розклад реєстратури пацієнту — 403", s == 403, {"status": s})

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK; results in {out / 'summary.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
