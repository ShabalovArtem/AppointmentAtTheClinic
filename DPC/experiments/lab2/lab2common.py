"""Спільне для дослідів DPC ЛР2: виклик POST /v1/compute, медіана й розкид, збереження результатів."""

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://localhost:8000"
TOKEN = "dev-token"
ROOT = Path(__file__).resolve().parents[3]


def utf8_stdout() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def call(method: str, path: str, body: dict | None = None, timeout: float = 600.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    return status, (json.loads(raw) if raw else None), time.perf_counter() - started


def compute(**body) -> dict:
    status, data, _ = call("POST", "/v1/compute", body)
    if status != 200 or data.get("state") != "done":
        raise SystemExit(f"compute failed: {status} {data}")
    return data["result"]


def stats(values: list[float]) -> dict:
    return {"median": statistics.median(values), "min": min(values), "max": max(values),
            "spread": max(values) - min(values), "values": values}


def save(out: str, name: str, data) -> Path:
    target = ROOT / out
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target / name
