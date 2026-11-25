"""Ліміт частоти змінювальних запитів (Web ЛР2, Д2.1), спільний для app-a і app-b.

Лічильник — у Redis (`rl:<user>:<хвилина>`), тож обидва екземпляри рахують в один лічильник.
INCR атомарний; перше збільшення ставить строк ключа. Понад RATE_LIMIT_PER_MIN за хвилину — 429
з Retry-After до початку наступної хвилини. Якщо Redis недоступний, запит пропускається (fail-open):
ліміт — захист від зловживань, а не умова правильності, і падіння Redis не має зупиняти запис.
"""

import time
from typing import Any

from redis.exceptions import RedisError

from . import cache, config
from .jsonlog import log
from .problems import Problem


async def check(user: dict[str, Any]) -> None:
    if config.RATE_LIMIT_PER_MIN <= 0:
        return
    now = time.time()
    window = int(now // 60)
    key = f"rl:{user['id']}:{window}"
    try:
        r = cache.client()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 70)
    except (RedisError, OSError) as exc:
        log("ratelimit.unavailable", error=type(exc).__name__)
        return
    if count > config.RATE_LIMIT_PER_MIN:
        retry_after = max(1, 60 - int(now % 60))
        raise Problem(429, "rate-limited", "Too many changing requests",
                      f"limit {config.RATE_LIMIT_PER_MIN} per minute", headers={"Retry-After": str(retry_after)})
