"""Спільне для дослідів Web ЛР2: HTTP без сторонніх залежностей, керування стендом, перцентилі."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

NGINX = "http://localhost:8080"
APP_A = "http://localhost:8001"
APP_B = "http://localhost:8002"
DPC = "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[3]  # корінь спільного проєкту (compose.yaml)


def utf8_stdout() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def call(base, method, path, body=None, token=None, headers=None, timeout=15.0):
    """-> (status, headers_lower, json|None, seconds); status 0 — відповіді немає (обрив з'єднання)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    except (OSError, urllib.error.URLError):
        return 0, {}, None, time.perf_counter() - started
    elapsed = time.perf_counter() - started
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, {k.lower(): v for k, v in hdrs.items()}, parsed, elapsed


def register(n: int, prefix: str) -> list[dict]:
    """Створює n тестових пацієнтів (реєстрація) і повертає [{id, token}]."""
    tag = uuid.uuid4().hex[:5]
    out = []
    for i in range(n):
        s, _, d, _ = call(NGINX, "POST", "/v1/auth/register",
                          {"login": f"{prefix}-{tag}-{i:03d}", "password": "experiment-pass", "name": f"Тест {i}"})
        if s != 201:
            raise SystemExit(f"registration failed: {s} {d}")
        out.append({"id": d["user"]["id"], "token": d["access_token"]})
    return out


def compose(*args: str, env: dict | None = None, check: bool = True) -> str:
    import os
    full_env = {**os.environ, **(env or {})}
    res = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=full_env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{res.stderr[-2000:]}")
    return res.stdout


def psql(sql: str) -> str:
    res = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-v",
                          "ON_ERROR_STOP=1", "-q", "-At"], cwd=ROOT, input=sql, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SystemExit(f"psql failed:\n{res.stderr[-2000:]}")
    return res.stdout.strip()


def restart_apps(env: dict) -> None:
    """Перезапуск app-a і app-b з режимом досліду та очікування готовності обох."""
    compose("up", "-d", "--no-deps", "--force-recreate", "app-a", "app-b", env=env)
    wait_ready()


def wait_ready(timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok = [call(base, "GET", "/ready", timeout=2)[0] == 200 for base in (APP_A, APP_B)]
        # nginx має бачити нові адреси (resolver valid=5s)
        seen = {(call(NGINX, "GET", "/health", timeout=2)[2] or {}).get("instance") for _ in range(6)}
        if all(ok) and seen >= {"app-a", "app-b"}:
            return
        time.sleep(1)
    raise SystemExit("app-a/app-b are not ready")


def resource(day: str) -> dict:
    """Стан ресурсу з частини DPC (спільна база): slots_taken, booked, invariant_ok."""
    s, _, d, _ = call(DPC, "GET", f"/v1/doctor-day/{day}", headers={"Authorization": "Bearer dev-token"})
    if s != 200:
        raise SystemExit(f"resource {day}: {s} {d}")
    return d


def reset_day(day: str) -> None:
    psql(f"""BEGIN;
DELETE FROM idempotency_key k USING booking b
 WHERE b.doctor_day_id = '{day}' AND k.response_body->>'id' = b.id;
DELETE FROM booking WHERE doctor_day_id = '{day}';
UPDATE doctor_day SET slots_taken = 0, closed = false WHERE id = '{day}';
COMMIT;""")


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, -(-q * len(ordered) // 100) - 1))]


def save(out: str, name: str, data) -> Path:
    target = ROOT / out
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target / name
