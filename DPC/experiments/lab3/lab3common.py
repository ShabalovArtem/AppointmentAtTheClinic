"""Спільне для дослідів DPC ЛР3: HTTP, керування стендом (compose, psql), очікування кінцевих станів."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://localhost:8000"
TOKEN = "dev-token"
ROOT = Path(__file__).resolve().parents[3]
FINAL = {"done", "rejected", "failed"}


def utf8_stdout() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def call(method, path, body=None, timeout=30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except OSError:
        return 0, None


def compose(*args, env=None, check=True) -> str:
    res = subprocess.run(["docker", "compose", *args], cwd=ROOT, env={**os.environ, **(env or {})},
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{res.stderr[-2000:]}")
    return res.stdout + res.stderr


def psql(sql: str) -> str:
    res = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-q", "-At",
                          "-v", "ON_ERROR_STOP=1"], cwd=ROOT, input=sql, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SystemExit(f"psql failed:\n{res.stderr[-2000:]}")
    return res.stdout.strip()


def restart_api(env: dict) -> None:
    compose("up", "-d", "--no-deps", "--force-recreate", "api", env=env)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        logs = compose("logs", "api", "--since", "2m", check=False)
        if call("GET", "/health")[0] == 200 and ("ray.connected" in logs or env.get("EXECUTION") == "local"):
            return
        time.sleep(1)
    raise SystemExit("api is not ready")


def wait_final(request_id: str, timeout: float = 120.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s, d = call("GET", f"/v1/booking-requests/{request_id}")
        if s == 200 and d["state"] in FINAL:
            return d
        time.sleep(0.3)
    return None


def resource(day: str) -> dict:
    return call("GET", f"/v1/doctor-day/{day}")[1]


def fault_hits(since: str = "5m") -> list[str]:
    """fault.hit пишеться в журнал процесу-виконавця Ray (stdout воркера → журнали Ray на вузлі)."""
    hits = []
    for svc in ("ray-worker", "ray-worker-extra", "ray-head"):
        out = compose("exec", "-T", svc, "sh", "-c",
                      "grep -rh 'fault.hit' /tmp/ray/session_latest/logs/ 2>/dev/null | tail -5", check=False)
        hits += [f"{svc}: {line}" for line in out.splitlines() if "fault.hit" in line]
    return hits


def save(out: str, name: str, data) -> Path:
    target = ROOT / out
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target / name
