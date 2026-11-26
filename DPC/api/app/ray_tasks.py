"""ЛР3: задача Ray, що обробляє один запит кол-центру, і диспетчер із межею незавершених задач.

Задача (виконується на вузлі Ray, ray-worker / ray-worker-extra):
  1. повільний виклик — перевірка поліса в страховій (SLOW_MIN_MS..SLOW_MAX_MS);
  2. публікація ефекту приймачу POST /internal/v1/effects з ключем request_id (однаковим у всіх спробах);
  3. fault("after_effect_before_ack") — керована відмова О3.4: приймач уже записав ефект, а задача ще не
     повернула результат; процес гине, Ray повторює задачу (max_retries), повтор отримує збережене рішення.
Ресурс задача НЕ змінює — лише приймач.

Диспетчер (у процесі api): бере запити з черги ЛР1 і подає задачі, доки незавершених менше за
IN_FLIGHT_LIMIT (О3.2); далі чекає ray.wait(..., num_returns=1) і обробляє ПЕРШИЙ готовий результат.
Запити понад межу лишаються в черзі ЛР1, а повна черга дає 429 — зворотний тиск доходить до клієнта.
"""

import os
import random
import socket
import time

import httpx
import ray

from . import config
from .faults import fault

_connected = False


def connect() -> None:
    global _connected
    if not _connected:
        ray.init(address=config.RAY_ADDRESS, logging_level="WARNING")
        _connected = True


@ray.remote(max_retries=config.RAY_TASK_MAX_RETRIES, retry_exceptions=False, num_cpus=config.RAY_TASK_CPUS)
def process_request(request_id: str, doctor_day_id: str, patient_id: str, slots: int, settings: dict) -> dict:
    started = time.time()
    worker = f"{socket.gethostname()}:{os.getpid()}"
    time.sleep(random.uniform(settings["slow_min_ms"], settings["slow_max_ms"]) / 1000)  # поліс у страховій
    policy_ok = not patient_id.startswith("invalid-")
    reply = httpx.post(
        settings["receiver_url"] + "/internal/v1/effects",
        json={"effect_key": request_id, "doctor_day_id": doctor_day_id, "patient_id": patient_id, "slots": slots,
              "policy_ok": policy_ok, "worker": worker},
        headers={"Authorization": f"Bearer {settings['token']}"},
        timeout=settings["receiver_timeout_s"],
    )
    reply.raise_for_status()
    decision = reply.json()
    fault("after_effect_before_ack")  # О3.4: ефект прийнято, підтвердження задачі ще немає
    return {**decision, "worker": worker, "task_s": round(time.time() - started, 3)}


def task_settings() -> dict:
    return {"slow_min_ms": config.SLOW_MIN_MS, "slow_max_ms": config.SLOW_MAX_MS, "receiver_url": config.RECEIVER_URL,
            "token": config.API_TOKEN, "receiver_timeout_s": config.RECEIVER_TIMEOUT_S}
