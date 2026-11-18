"""Спільне для дослідів ЛР1: HTTP без сторонніх залежностей, перцентилі, запис результатів."""

import http.client
import json
import threading
import time
import urllib.parse
from pathlib import Path

FINAL = {"done", "rejected", "failed"}


class Api:
    def __init__(self, base: str, token: str, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.prefix = urllib.parse.urlsplit(self.base).path
        self.token = token
        self.timeout = timeout
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        """Одне keep-alive з'єднання на потік: інакше тисячі коротких TCP-з'єднань вичерпують порти клієнта."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            url = urllib.parse.urlsplit(self.base)
            conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict, dict | None, float]:
        """-> (status, headers, json, seconds). status 0 — відповіді немає."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        started = time.perf_counter()
        for attempt in (1, 2):  # повтор лише якщо сервер закрив простоюване keep-alive з'єднання
            conn = self._conn()
            try:
                conn.request(method, self.prefix + path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status, resp_headers = resp.status, {k.lower(): v for k, v in resp.getheaders()}
                break
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                self._local.conn = None
                stale = isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError))
                if attempt == 2 or not stale:
                    return 0, {}, None, time.perf_counter() - started
        elapsed = time.perf_counter() - started
        headers = resp_headers
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, headers, parsed, elapsed

    def wait_final(self, request_id: str, timeout: float = 300.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, data, _ = self.call("GET", f"/v1/booking-requests/{request_id}")
            if status == 200 and data and data["state"] in FINAL:
                return data
            time.sleep(0.2)
        return None


def percentile(values: list[float], q: int) -> float | None:
    """Метод найближчого рангу: значення, не менше за q % вибірки."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, -(-q * len(ordered) // 100) - 1))
    return ordered[index]


def out_dir(path: str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def dump(target: Path, name: str, data: object) -> None:
    (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
