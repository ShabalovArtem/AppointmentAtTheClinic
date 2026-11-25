"""Ідемпотентність головної операції (Web ЛР2, О2.2).

Що зберігається (таблиця idempotency_key): користувач, операція, ключ, відбиток тіла, стан,
відповідь (статус, тіло, заголовки) і строк дії.

Атомарність. `claim()` вставляє рядок ключа в тій самій транзакції, що виконує предметну зміну, а
`finish()` записує в нього відповідь перед COMMIT. Отже ключ і запис пацієнта фіксуються разом:
не буває ані ключа без зміни, ані зміни без ключа. Окремої перевірки «чи є ключ» перед вставкою
немає — вставка з ON CONFLICT сама і перевіряє, і займає ключ (типова помилка «перевірити, потім
вставити» виключена).

Дублікат під час обробки. Другий запит із тим самим ключем, поки перша транзакція не зафіксована,
чекає на унікальному індексі. Очікування обмежене `lock_timeout` (IDEMPOTENCY_WAIT_MS): не дочекався —
409 «ключ ще обробляється» з Retry-After; дочекався — отримує збережену відповідь.

Відбиток — SHA-256 канонічного JSON перевіреного тіла (ключі відсортовано, без пробілів), тож
перестановка полів чи `"visit_reason": null` замість відсутнього поля відбиток не змінюють.
"""

import asyncio
import hashlib
import json
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from . import config, db
from .jsonlog import log
from .problems import Problem

CREATE_BOOKING = "create_booking"


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise Problem(400, "idempotency-key-required", "Idempotency-Key header is required",
                      "send a new random key (e.g. a UUID) and repeat it unchanged on every retry")
    key = value.strip()
    if not 8 <= len(key) <= 128 or not key.isprintable():
        raise Problem(400, "invalid-idempotency-key", "Invalid Idempotency-Key", "8..128 printable characters")
    return key


async def claim(conn: AsyncConnection, user_id: str, operation: str, key: str, request_hash: str) -> dict[str, Any] | None:
    """Займає ключ у поточній транзакції. None — ключ новий (виконуй операцію й виклич finish);
    dict — збережена відповідь попереднього виконання (поверни її без повторної зміни)."""
    # обмежене очікування лише на вставці ключа; далі — звичайні тайм-аути (рядок дня лікаря тощо)
    await conn.execute(f"SET LOCAL lock_timeout = '{int(config.IDEMPOTENCY_WAIT_MS)}ms'")
    claimed = await (
        await conn.execute(
            """INSERT INTO idempotency_key (user_id, operation, key, request_hash, state, expires_at)
               VALUES (%s, %s, %s, %s, 'processing', now() + make_interval(hours => %s))
               ON CONFLICT (user_id, operation, key) DO UPDATE
                 SET request_hash = EXCLUDED.request_hash, state = 'processing', response_status = NULL,
                     response_body = NULL, response_headers = '{}'::jsonb, created_at = now(),
                     expires_at = EXCLUDED.expires_at
                 WHERE idempotency_key.expires_at <= now()   -- прострочений ключ займається заново
               RETURNING key""",
            (user_id, operation, key, request_hash, config.IDEMPOTENCY_TTL_HOURS),
        )
    ).fetchone()
    await conn.execute("SET LOCAL lock_timeout = 0")
    if claimed is not None:
        return None

    stored = await (
        await conn.execute(
            """SELECT request_hash, state, response_status, response_body, response_headers
               FROM idempotency_key WHERE user_id = %s AND operation = %s AND key = %s""",
            (user_id, operation, key),
        )
    ).fetchone()
    if stored["request_hash"] != request_hash:
        raise Problem(422, "idempotency-key-reused", "Idempotency-Key was used with a different request body",
                      "use a new key for a different request")
    if stored["state"] != "done":  # не трапляється: ключ і відповідь фіксуються разом
        raise Problem(409, "idempotency-key-in-progress", "Request with this key is still being processed",
                      headers={"Retry-After": "1"})
    return stored


async def finish(conn: AsyncConnection, user_id: str, operation: str, key: str,
                 status: int, body: dict[str, Any], headers: dict[str, str]) -> None:
    await conn.execute(
        """UPDATE idempotency_key SET state = 'done', response_status = %s, response_body = %s, response_headers = %s
           WHERE user_id = %s AND operation = %s AND key = %s""",
        (status, Jsonb(body), Jsonb(headers), user_id, operation, key),
    )


def in_progress() -> Problem:
    return Problem(409, "idempotency-key-in-progress", "Request with this key is still being processed",
                   "repeat with the same key after Retry-After", headers={"Retry-After": "1"})


LockTimeout = psycopg.errors.LockNotAvailable


async def cleanup_loop() -> None:
    """Прибирання прострочених ключів порціями по IDEMPOTENCY_CLEANUP_BATCH рядків.
    Працює в кожному екземплярі; SKIP LOCKED не дає двом екземплярам чекати один на одного."""
    while True:
        await asyncio.sleep(config.IDEMPOTENCY_CLEANUP_S)
        total = batches = 0
        try:
            while True:
                async with db.connection() as conn:
                    cur = await conn.execute(
                        """DELETE FROM idempotency_key WHERE ctid = ANY(ARRAY(
                             SELECT ctid FROM idempotency_key WHERE expires_at < now()
                             LIMIT %s FOR UPDATE SKIP LOCKED))""",
                        (config.IDEMPOTENCY_CLEANUP_BATCH,),
                    )
                    deleted = cur.rowcount
                total += deleted
                batches += 1
                if deleted < config.IDEMPOTENCY_CLEANUP_BATCH:
                    break
        except (psycopg.Error, OSError) as exc:
            log("idempotency.cleanup_failed", error=type(exc).__name__)
        if total:
            log("idempotency.cleanup", deleted=total, batches=batches, batch_size=config.IDEMPOTENCY_CLEANUP_BATCH)
