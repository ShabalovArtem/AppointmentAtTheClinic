"""ЛР3 (О3.3): приймач ефекту задач Ray — POST /internal/v1/effects.

Предметний ресурс змінює ПРИЙМАЧ, а не виконавець Ray: задача лише перевіряє поліс (повільний виклик)
і публікує ефект сюди. Ключ ефекту — request_id, однаковий у всіх спробах однієї задачі.

Одна транзакція:
  1. INSERT INTO effect ... ON CONFLICT (effect_key) DO UPDATE SET deliveries = deliveries + 1 RETURNING ...
     — займає ключ або, якщо він уже є, блокує його рядок і рахує повторну доставку (захист від повтору);
  2. перша доставка: take_slots() + INSERT booking у точці збереження (зміна ресурсу; правило спільне з Web),
     рішення в effect і стан booking_request;
  3. повторна доставка: повертає збережене рішення, ресурс не змінює.
Одночасні дублікати чекають на рядку ключа, тож зміна ресурсу відбувається рівно один раз.
"""

import asyncio
from typing import Any

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import config, db
from .auth import require_token
from .jsonlog import log

router = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_token)])

ID = r"^[A-Za-z0-9._:-]{1,128}$"


class EffectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect_key: str = Field(pattern=ID)  # = request_id
    doctor_day_id: str = Field(pattern=ID)
    patient_id: str = Field(pattern=ID)
    slots: int = Field(ge=1, le=2)
    policy_ok: bool  # результат повільного виклику (перевірка поліса в страховій)
    worker: str | None = Field(default=None, max_length=128)  # вузол Ray, що доставив ефект


_STORED = "decision, reason, booking_id, deliveries"


@router.post("/effects")
async def receive_effect(body: EffectIn) -> dict[str, Any]:
    if config.RECEIVER_DELAY_MS:  # Д3.4: повільний приймач
        await asyncio.sleep(config.RECEIVER_DELAY_MS / 1000)
    async with db.connection() as conn, conn.transaction():
        row = await (
            await conn.execute(
                f"""INSERT INTO effect (effect_key, doctor_day_id, patient_id, slots, first_worker, last_worker)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (effect_key) DO UPDATE
                      SET deliveries = effect.deliveries + 1, last_delivery_at = now(), last_worker = EXCLUDED.last_worker
                    RETURNING {_STORED}""",
                (body.effect_key, body.doctor_day_id, body.patient_id, body.slots, body.worker, body.worker),
            )
        ).fetchone()
        if row["deliveries"] > 1:
            # повторна доставка (повтор задачі після відмови або дублікат): збережене рішення, без зміни ресурсу
            log("effect.duplicate", effect_key=body.effect_key, deliveries=row["deliveries"], decision=row["decision"])
            return {"effect_key": body.effect_key, "decision": row["decision"], "reason": row["reason"],
                    "booking_id": row["booking_id"], "deliveries": row["deliveries"], "duplicate": True}

        decision, reason, booking_id = "rejected", None, None
        if not body.policy_ok:
            reason = "policy-invalid"
        else:
            try:
                async with conn.transaction():  # SAVEPOINT: інваріант 2 відкочує й зайняті слоти
                    reason = (
                        await (
                            await conn.execute("SELECT take_slots(%s, %s) AS reason", (body.doctor_day_id, body.slots))
                        ).fetchone()
                    )["reason"]
                    if reason is None:
                        await conn.execute(
                            """INSERT INTO booking (id, channel, request_id, patient_id, doctor_day_id, slots, status)
                               VALUES (%s, 'call-center', %s, %s, %s, %s, 'confirmed')""",
                            (body.effect_key, body.effect_key, body.patient_id, body.doctor_day_id, body.slots),
                        )
                        decision, booking_id = "done", body.effect_key
            except psycopg.errors.UniqueViolation:
                reason = "patient-already-booked"
        await conn.execute(
            "UPDATE effect SET decision = %s, reason = %s, booking_id = %s WHERE effect_key = %s",
            (decision, reason, booking_id, body.effect_key),
        )
        await conn.execute(
            """UPDATE booking_request SET state = %s, reason = %s, finished_at = now()
               WHERE id = %s AND state NOT IN ('done', 'rejected')""",
            (decision, reason, body.effect_key),
        )
    log("effect.applied", effect_key=body.effect_key, decision=decision, reason=reason, worker=body.worker)
    return {"effect_key": body.effect_key, "decision": decision, "reason": reason, "booking_id": booking_id,
            "deliveries": 1, "duplicate": False}
