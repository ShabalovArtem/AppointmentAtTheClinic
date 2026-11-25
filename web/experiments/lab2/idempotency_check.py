"""О2.2 (механізм ідемпотентності) і Д2.1 (спільний ліміт частоти) — перевірка через обидва екземпляри.

Із кореня проєкту:  python web/experiments/lab2/idempotency_check.py
Прибирання прострочених ключів перевіряється наприкінці: 1 200 штучно прострочених ключів мають
зникнути порціями по 500 протягом одного циклу прибирання (до ~70 с); --skip-cleanup пропускає.
"""

import argparse
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

results: list[dict] = []


def check(name, ok, evidence):
    results.append({"check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="web/results/lab2/idempotency")
    ap.add_argument("--skip-cleanup", action="store_true")
    a = ap.parse_args()
    c.wait_ready()
    c.reset_day("web-retry")
    p1, p2 = c.register(2, "idem")
    day = "web-retry"

    def post(base, token, body, key=None, extra=None):
        headers = {**({"Idempotency-Key": key} if key else {}), **(extra or {})}
        return c.call(base, "POST", "/v1/bookings", body, token=token, headers=headers)

    body = {"doctor_day_id": day, "slots": 1, "visit_reason": "огляд"}
    s, _, d, _ = post(c.NGINX, p1["token"], body)
    check("немає Idempotency-Key — 400", s == 400 and d["type"].endswith("idempotency-key-required"), {"status": s})
    s, _, d, _ = post(c.NGINX, p1["token"], body, key="short")
    check("ключ коротший за 8 символів — 400", s == 400, {"status": s})

    key = f"idem-{uuid.uuid4()}"
    s1, h1, d1, _ = post(c.APP_A, p1["token"], body, key)
    s2, h2, d2, _ = post(c.APP_B, p1["token"], {"visit_reason": "огляд", "slots": 1, "doctor_day_id": day}, key)
    check("повтор із тим самим тілом через інший екземпляр — та сама відповідь",
          s1 == 201 and s2 == 201 and d1["id"] == d2["id"] and h1.get("location") == h2.get("location")
          and h2.get("idempotent-replayed") == "true",
          {"first": [s1, h1.get("x-instance"), d1.get("id")], "repeat": [s2, h2.get("x-instance"), d2.get("id")]})
    s, _, d, _ = post(c.APP_A, p1["token"], {**body, "slots": 2}, key)
    check("той самий ключ з іншим тілом — 422", s == 422 and d["type"].endswith("idempotency-key-reused"), {"status": s})
    s, h, d, _ = post(c.APP_B, p2["token"], body, key)
    check("той самий ключ іншого користувача — окрема операція (201, інший id)",
          s == 201 and d["id"] != d1["id"] and "idempotent-replayed" not in h, {"status": s})

    # відмова за правилом теж зберігається: повтор не виконує перевірку вдруге
    key2 = f"idem-{uuid.uuid4()}"
    r1 = post(c.APP_A, p1["token"], body, key2)
    r2 = post(c.APP_B, p1["token"], body, key2)
    check("збережена відмова 409 повторюється з тим самим тілом",
          r1[0] == 409 and r2[0] == 409 and r1[2] == r2[2] and r2[1].get("idempotent-replayed") == "true",
          {"first": r1[0], "repeat": r2[0], "type": r1[2].get("type")})

    # одночасні дублікати з тим самим ключем через обидва екземпляри
    c.reset_day(day)
    key3 = f"idem-{uuid.uuid4()}"
    out: list = []
    barrier = threading.Barrier(12)

    def dup(i):
        barrier.wait()
        out.append(post(c.APP_A if i % 2 else c.APP_B, p1["token"], body, key3))

    ts = [threading.Thread(target=dup, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = {r[2].get("id") for r in out if r[0] == 201}
    count = int(c.psql(f"SELECT count(*) FROM booking WHERE patient_id = '{p1['id']}' AND doctor_day_id = '{day}'"))
    statuses = Counter(r[0] for r in out)
    check("12 одночасних дублікатів: один запис, усі 201 — той самий id, інші — 409 «ключ обробляється»",
          count == 1 and len(ids) == 1 and set(statuses) <= {201, 409}
          and all(r[2]["type"].endswith("idempotency-key-in-progress") and r[1].get("retry-after") for r in out if r[0] == 409),
          {"statuses": dict(statuses), "distinct_ids": len(ids), "bookings": count,
           "replayed": sum(1 for r in out if r[1].get("idempotent-replayed") == "true")})
    stored = c.psql(f"""SELECT state || ' ' || response_status || ' ' || (expires_at - created_at)::text
                        FROM idempotency_key WHERE user_id = '{p1['id']}' AND key = '{key3}'""")
    check("ключ збережено: стан done, відповідь 201, строк 24 год", stored.startswith("done 201 1 day"), {"row": stored})

    # Д2.1: спільний ліміт частоти змінювальних запитів (120 на хвилину на користувача)
    p3 = c.register(1, "rate")[0]
    codes = []
    for i in range(130):
        base = c.APP_A if i % 2 else c.APP_B
        s, h, _, _ = post(base, p3["token"], {"doctor_day_id": "dd-main", "slots": 1}, f"rate-{uuid.uuid4()}")
        codes.append((s, h.get("retry-after"), h.get("x-instance")))
    limited = [x for x in codes if x[0] == 429]
    check("Д2.1: ліміт 120/хв спільний для app-a і app-b — далі 429 з Retry-After",
          len(limited) >= 1 and sum(1 for x in codes if x[0] != 429) <= 120 and all(x[1] for x in limited)
          and {x[2] for x in limited} >= {"app-a", "app-b"},
          {"not_limited": sum(1 for x in codes if x[0] != 429), "limited": len(limited),
           "limited_by_instance": dict(Counter(x[2] for x in limited)), "retry_after_example": limited[0][1] if limited else None})

    if not a.skip_cleanup:
        c.psql(f"""INSERT INTO idempotency_key (user_id, operation, key, request_hash, state, response_status,
                     response_body, created_at, expires_at)
                   SELECT '{p2['id']}', 'create_booking', 'expired-' || g, 'x', 'done', 201, '{{}}'::jsonb,
                          now() - interval '2 days', now() - interval '1 day'
                   FROM generate_series(1, 1200) g""")
        before = int(c.psql("SELECT count(*) FROM idempotency_key WHERE key LIKE 'expired-%'"))
        deadline = time.monotonic() + 75
        left = before
        while time.monotonic() < deadline and left:
            time.sleep(3)
            left = int(c.psql("SELECT count(*) FROM idempotency_key WHERE key LIKE 'expired-%'"))
        logs = c.compose("logs", "app-a", "app-b", "--since", "3m", check=False)
        line = next((l for l in reversed(logs.splitlines()) if "idempotency.cleanup" in l and '"deleted": 1200' in l), None)
        check("прострочені ключі прибрано порціями (1 200 → 0, порції по 500)", left == 0,
              {"before": before, "left": left, "log": line})

    path = c.save(a.out, "summary.json", results)
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK -> {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
