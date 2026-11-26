"""C02 «Запис до клініки», ЛР1: приймання запитів на запис, обмежена черга, пул обробників,
спільний ресурс doctor_day і час виконання.

Шлях запиту:
  POST /v1/booking-requests -> put_nowait у чергу (429 + Retry-After, якщо місця немає) -> 202
  обробник: queue.get() -> processing -> перевірка поліса (повільний виклик, поза критичною секцією)
            -> критична секція (перевірка вільних слотів + зміна slots_taken + запис booking) -> done | rejected
  GET /v1/booking-requests/{id} -> стан; перша видача кінцевого стану фіксує позначку first_read.

Примітив синхронізації черги — asyncio.Queue(maxsize=QUEUE_CAPACITY). Порожня черга: обробник
чекає в `await queue.get()` на future, який будить `put_nowait`; цикл подій у цей час вільний
(немає активного очікування). Повна черга: `put_nowait` одразу кидає QueueFull — перевірка місця
й постановка в чергу є однією синхронною дією без `await` між ними, тож гонки «перевірив — записав» немає.

Захист ресурсу (О1.4): RESOURCE_MODE=locked — транзакція PostgreSQL з `SELECT ... FOR UPDATE`
(блокування рядка doctor_day), у якій перевірка, зміна slots_taken, запис booking і стану запиту.
RESOURCE_MODE=unprotected — читання без блокування й запис абсолютного значення окремим кроком
(лише на тестових даних race-unprotected).
"""

import asyncio
import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Response
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from . import config, db, metrics
from .auth import require_token
from .jsonlog import log
from .problems import Problem

FINAL = {"done", "rejected", "failed"}
# стани бронювання, що займають слоти (спільна таблиця booking частин Web і DPC)
OCCUPYING = "('held', 'confirmed', 'visited', 'no_show')"
ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


class BookingCreate(BaseModel):
    request_id: str = Field(pattern=ID_PATTERN)
    doctor_day_id: str = Field(pattern=ID_PATTERN)
    patient_id: str = Field(pattern=ID_PATTERN)
    slots: int = Field(default=1, ge=1, le=2)  # картка: 1–2 слоти


@dataclass
class Job:
    request_id: str
    doctor_day_id: str
    patient_id: str
    slots: int
    persisted: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False


@dataclass
class Timing:
    """Позначки монотонного годинника (time.monotonic, секунди) одного запиту."""

    received: float
    started: float | None = None
    finished: float | None = None
    first_read: float | None = None

    def intervals_ms(self) -> dict[str, float | None]:
        def ms(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else round((b - a) * 1000, 1)

        return {
            "queue_wait_ms": ms(self.received, self.started),
            "processing_ms": ms(self.started, self.finished),
            "delivery_wait_ms": ms(self.finished, self.first_read),
            "total_ms": ms(self.received, self.finished),
        }


class Stats:
    """Лічильники й вибірки для /metrics. Усе змінюється в одному циклі подій — блокування не потрібне."""

    SAMPLE = 2000  # останні N завершених запитів

    def __init__(self) -> None:
        self.accepted = 0
        self.rejected_overload = 0  # 429: черга повна, запит не прийнято
        self.rejected_shutdown = 0  # 503 під час завершення роботи
        self.outcomes = {"done": 0, "rejected": 0, "failed": 0}
        self.busy = 0  # обробники, що зараз обробляють запит (у режимі ray — незавершені задачі Ray)
        self.submitted = 0  # подано задач Ray (О3.2)
        self.max_in_flight = 0  # найбільша кількість незавершених задач Ray одночасно
        self.order_violations = 0
        self.samples: dict[str, deque[float]] = {
            name: deque(maxlen=self.SAMPLE) for name in ("queue_wait_ms", "processing_ms", "total_ms", "delivery_wait_ms")
        }

    def add(self, name: str, value: float | None) -> None:
        if value is not None:
            self.samples[name].append(value)

    def percentiles(self) -> dict[str, dict[str, float | int | None]]:
        out: dict[str, dict[str, float | int | None]] = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            out[name] = {"n": len(ordered), **{f"p{q}": _nearest_rank(ordered, q) for q in (50, 95, 99)}}
        return out


def _nearest_rank(ordered: list[float], q: int) -> float | None:
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, -(-q * len(ordered) // 100) - 1))  # ceil(q*n/100) - 1
    return ordered[index]


queue: asyncio.Queue[Job] | None = None
stats = Stats()
timings: OrderedDict[str, Timing] = OrderedDict()
TIMINGS_MAX = 200_000
_workers: list[asyncio.Task[None]] = []
_accepting = False


# lifecycle (called from main.lifespan)


def start() -> None:
    global queue, _accepting
    queue = asyncio.Queue(maxsize=config.QUEUE_CAPACITY)
    if config.EXECUTION == "ray":  # ЛР3: обробка в задачах Ray, межа незавершених задач IN_FLIGHT_LIMIT
        _workers.append(asyncio.create_task(_ray_dispatcher(), name="ray-dispatcher"))
    else:  # ЛР1: локальний пул WORKERS обробників
        _workers.extend(asyncio.create_task(_worker(n), name=f"worker-{n}") for n in range(config.WORKERS))
    _accepting = True

    metrics.gauge("queue_length", lambda: queue.qsize() if queue else 0)
    metrics.gauge("queue_capacity", lambda: config.QUEUE_CAPACITY)
    metrics.gauge("workers", lambda: config.WORKERS)
    metrics.gauge("processing", lambda: stats.busy)
    metrics.gauge("accepted", lambda: stats.accepted)
    metrics.gauge("rejected", lambda: stats.rejected_overload)
    metrics.gauge("rejected_shutdown", lambda: stats.rejected_shutdown)
    metrics.gauge("outcomes", lambda: dict(stats.outcomes))
    metrics.gauge("timing_order_violations", lambda: stats.order_violations)
    metrics.gauge("timing_ms", stats.percentiles)
    metrics.gauge("resource_mode", lambda: config.RESOURCE_MODE)
    metrics.gauge("execution", lambda: config.EXECUTION)
    metrics.gauge("in_flight_limit", lambda: config.IN_FLIGHT_LIMIT)
    metrics.gauge("ray_in_flight", lambda: stats.busy if config.EXECUTION == "ray" else 0)
    metrics.gauge("ray_submitted", lambda: stats.submitted)
    metrics.gauge("ray_max_in_flight", lambda: stats.max_in_flight)
    log(
        "booking.started",
        queue_capacity=config.QUEUE_CAPACITY,
        workers=config.WORKERS,
        resource_mode=config.RESOURCE_MODE,
        execution=config.EXECUTION,
        in_flight_limit=config.IN_FLIGHT_LIMIT,
    )


async def stop() -> None:
    """Нові запити отримують 503; прийняті обробляються до кінця, потім обробники зупиняються."""
    global _accepting
    _accepting = False
    if queue is not None:
        left = queue.qsize() + stats.busy
        log("booking.draining", left=left)
        try:
            await asyncio.wait_for(queue.join(), timeout=config.DRAIN_TIMEOUT_S)
            log("booking.drained", processed=left)
        except TimeoutError:
            log("booking.drain_timeout", left=queue.qsize() + stats.busy)
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)


# routes


@router.post("/booking-requests", status_code=202)
async def create_booking_request(body: BookingCreate, response: Response) -> dict[str, object]:
    received = time.monotonic()
    if not _accepting or queue is None:
        stats.rejected_shutdown += 1
        raise Problem(503, "shutting-down", "Service is shutting down", headers={"Retry-After": "5"})

    async with db.connection() as conn:
        found = await (await conn.execute("SELECT 1 FROM doctor_day WHERE id = %s", (body.doctor_day_id,))).fetchone()
    if found is None:
        raise Problem(404, "doctor-day-not-found", "Doctor day not found", f"doctor_day_id={body.doctor_day_id}")

    job = Job(body.request_id, body.doctor_day_id, body.patient_id, body.slots)
    try:
        queue.put_nowait(job)  # місце в черзі займається тут, атомарно відносно інших корутин
    except asyncio.QueueFull:
        stats.rejected_overload += 1
        raise Problem(
            429,
            "queue-full",
            "Booking queue is full",
            f"capacity={config.QUEUE_CAPACITY}",
            headers={"Retry-After": str(config.RETRY_AFTER_S)},
        ) from None

    try:
        async with db.connection() as conn:
            inserted = await (
                await conn.execute(
                    """INSERT INTO booking_request (id, doctor_day_id, patient_id, slots, state)
                       VALUES (%s, %s, %s, %s, 'queued') ON CONFLICT (id) DO NOTHING RETURNING id""",
                    (body.request_id, body.doctor_day_id, body.patient_id, body.slots),
                )
            ).fetchone()
    except BaseException:
        job.dropped = True
        job.persisted.set()
        raise
    if inserted is None:
        job.dropped = True
        job.persisted.set()
        raise Problem(409, "duplicate-request", "Request id already used", f"request_id={body.request_id}")

    _remember(body.request_id, Timing(received=received))
    stats.accepted += 1
    job.persisted.set()
    log("booking.accepted", request_id=body.request_id, doctor_day_id=body.doctor_day_id, queue_length=queue.qsize())

    location = f"/v1/booking-requests/{body.request_id}"
    response.headers["Location"] = location
    return {"id": body.request_id, "state": "queued", "location": location}


@router.get("/booking-requests/{request_id}")
async def get_booking_request(request_id: str) -> dict[str, object]:
    async with db.connection() as conn:
        row = await (
            await conn.execute(
                """SELECT id, doctor_day_id, patient_id, slots, state, reason,
                          received_at, started_at, finished_at, first_read_at
                   FROM booking_request WHERE id = %s""",
                (request_id,),
            )
        ).fetchone()
        if row is None:
            raise Problem(404, "not-found", "Booking request not found")

        timing = timings.get(request_id)
        if row["state"] in FINAL and row["first_read_at"] is None:
            # перша видача кінцевого результату: позначка ставиться лише тепер, не раніше
            await conn.execute(
                "UPDATE booking_request SET first_read_at = now() WHERE id = %s AND first_read_at IS NULL",
                (request_id,),
            )
            if timing is not None and timing.first_read is None:
                timing.first_read = time.monotonic()
                _check_order(timing)
                stats.add("delivery_wait_ms", timing.intervals_ms()["delivery_wait_ms"])

    result: dict[str, object] = {
        "id": row["id"],
        "doctor_day_id": row["doctor_day_id"],
        "patient_id": row["patient_id"],
        "slots": row["slots"],
        "state": row["state"],
        "reason": row["reason"],
        "received_at": row["received_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    if timing is not None:
        result["timing"] = timing.intervals_ms()
    return result


@router.get("/doctor-days")
async def list_doctor_days() -> dict[str, object]:
    """Довідник днів лікарів для клієнта (вибір ресурсу)."""
    async with db.connection() as conn:
        rows = await (
            await conn.execute(
                """SELECT d.id, doc.full_name, doc.specialty, d.day, d.slots_total, d.slots_taken
                   FROM doctor_day d JOIN doctor doc ON doc.id = d.doctor_id ORDER BY d.id"""
            )
        ).fetchall()
    return {
        "items": [
            {**r, "day": r["day"].isoformat(), "slots_free": r["slots_total"] - r["slots_taken"]} for r in rows
        ]
    }


@router.get("/doctor-day/{doctor_day_id}")
async def get_doctor_day(doctor_day_id: str) -> dict[str, object]:
    async with db.connection() as conn:
        row = await (
            await conn.execute(
                f"""SELECT d.id, d.doctor_id, doc.specialty, d.day, d.slots_total, d.slots_taken, d.closed,
                          (SELECT coalesce(sum(b.slots), 0)::int FROM booking b
                            WHERE b.doctor_day_id = d.id AND b.status IN {OCCUPYING}) AS booked,
                          (SELECT count(*)::int FROM booking b
                            WHERE b.doctor_day_id = d.id AND b.status IN {OCCUPYING}) AS bookings,
                          (SELECT count(*)::int FROM booking b
                            WHERE b.doctor_day_id = d.id AND b.status IN {OCCUPYING} AND b.channel = 'web') AS web_bookings
                   FROM doctor_day d JOIN doctor doc ON doc.id = d.doctor_id
                   WHERE d.id = %s""",
                (doctor_day_id,),
            )
        ).fetchone()
    if row is None:
        raise Problem(404, "not-found", "Doctor day not found")

    return {
        "id": row["id"],
        "doctor_id": row["doctor_id"],
        "specialty": row["specialty"],
        "day": row["day"].isoformat(),
        "slots_total": row["slots_total"],
        "slots_taken": row["slots_taken"],
        "slots_free": row["slots_total"] - row["slots_taken"],
        "closed": row["closed"],
        "booked": row["booked"],
        "bookings": row["bookings"],
        "web_bookings": row["web_bookings"],  # записи сайту (частина Web) на той самий ресурс
        "invariant_ok": row["slots_taken"] == row["booked"] and row["booked"] <= row["slots_total"],
    }


# workers


async def _worker(n: int) -> None:
    assert queue is not None
    while True:
        job = await queue.get()  # порожня черга: корутина спить на future, доки put_nowait її не розбудить
        try:
            await job.persisted.wait()  # рядок запиту вже записано обробником маршруту
            if not job.dropped:
                stats.busy += 1
                try:
                    await _process(job, n)
                finally:
                    stats.busy -= 1
        except Exception as exc:  # noqa: BLE001 - обробник не має зупинятися
            log("booking.worker_error", worker=n, error=type(exc).__name__)
        finally:
            queue.task_done()


async def _process(job: Job, worker: int) -> None:
    timing = timings.get(job.request_id)
    started = time.monotonic()
    if timing is not None:
        timing.started = started
    try:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE booking_request SET state = 'processing', started_at = now() WHERE id = %s",
                (job.request_id,),
            )

        # Повільний виклик — поза критичною секцією й без блокування циклу подій.
        policy_ok = await check_policy(job.patient_id)
        if not policy_ok:
            await _finish(job, "rejected", "policy-invalid")
        elif config.RESOURCE_MODE == "unprotected":
            await _book_unprotected(job)
        else:
            await _book_locked(job)
    except Exception as exc:  # noqa: BLE001 - помилка оброблення -> failed
        log("booking.failed", request_id=job.request_id, error=type(exc).__name__)
        try:
            await _finish(job, "failed", type(exc).__name__)
        except Exception:  # noqa: BLE001 - база недоступна: стан лишиться processing до відновлення
            log("booking.state_lost", request_id=job.request_id)


async def check_policy(patient_id: str) -> bool:
    """Повільний виклик із картки: перевірка поліса в страховій, SLOW_MIN_MS..SLOW_MAX_MS.
    Імітація: недійсні поліси мають пацієнти з ідентифікатором `invalid-...`."""
    await asyncio.sleep(random.uniform(config.SLOW_MIN_MS, config.SLOW_MAX_MS) / 1000)
    return not patient_id.startswith("invalid-")


_INSERT_BOOKING = """INSERT INTO booking (id, channel, request_id, patient_id, doctor_day_id, slots, status)
                     VALUES (%s, 'call-center', %s, %s, %s, %s, 'confirmed')"""


async def _book_locked(job: Job) -> None:
    """Критична секція: блокування рядка doctor_day від перевірки до фіксації транзакції.

    Перевірку й зміну виконує спільна з Web функція бази take_slots() (одне предметне правило для
    сайту й кол-центру); бронювання й стан запиту записуються в тій самій транзакції."""
    try:
        async with db.connection() as conn, conn.transaction():
            # блокування береться тут, щоб пауза CS_GAP_MS (дослід О1.4) була всередині критичної секції
            await conn.execute("SELECT 1 FROM doctor_day WHERE id = %s FOR UPDATE", (job.doctor_day_id,))
            if config.CS_GAP_MS:
                await asyncio.sleep(config.CS_GAP_MS / 1000)
            reason = (
                await (
                    await conn.execute("SELECT take_slots(%s, %s) AS reason", (job.doctor_day_id, job.slots))
                ).fetchone()
            )["reason"]
            if reason is None:
                await conn.execute(
                    _INSERT_BOOKING, (job.request_id, job.request_id, job.patient_id, job.doctor_day_id, job.slots)
                )
            state = "done" if reason is None else "rejected"
            await _set_state(conn, job, state, reason)
    except psycopg.errors.UniqueViolation:
        # інваріант 2: у пацієнта вже є активний запис до цього лікаря на цей день; транзакцію
        # (разом із зайнятими слотами) відкочено
        await _finish(job, "rejected", "patient-already-booked")
        return
    _mark_finished(job, state)  # після фіксації транзакції


async def _book_unprotected(job: Job) -> None:
    """Навмисно хибний варіант (лише тестові дані): перевірка й зміна — окремі кроки без блокування,
    а записується абсолютне значення, прочитане раніше. Паралельні обробники гублять оновлення
    й займають більше слотів, ніж є."""
    async with db.connection() as conn:
        day = await (
            await conn.execute(
                "SELECT slots_total, slots_taken, closed FROM doctor_day WHERE id = %s", (job.doctor_day_id,)
            )
        ).fetchone()
        await conn.commit()
        reason = "day-closed" if day["closed"] else None
        if reason is None and day["slots_taken"] + job.slots > day["slots_total"]:
            reason = "no-free-slots"
        if reason is not None:
            await _set_state(conn, job, "rejected", reason)
            await conn.commit()
            _mark_finished(job, "rejected")
            return
        if config.CS_GAP_MS:
            await asyncio.sleep(config.CS_GAP_MS / 1000)
        try:
            await conn.execute(
                "UPDATE doctor_day SET slots_taken = %s WHERE id = %s",
                (day["slots_taken"] + job.slots, job.doctor_day_id),
            )
            await conn.execute(
                _INSERT_BOOKING, (job.request_id, job.request_id, job.patient_id, job.doctor_day_id, job.slots)
            )
            await _set_state(conn, job, "done", None)
        except psycopg.errors.UniqueViolation:
            await conn.rollback()
            await _set_state(conn, job, "rejected", "patient-already-booked")
            await conn.commit()
            _mark_finished(job, "rejected")
            return
    _mark_finished(job, "done")


async def _finish(job: Job, state: str, reason: str | None) -> None:
    async with db.connection() as conn:
        await _set_state(conn, job, state, reason)
    _mark_finished(job, state)


async def _set_state(conn: AsyncConnection, job: Job, state: str, reason: str | None) -> None:
    await conn.execute(
        "UPDATE booking_request SET state = %s, reason = %s, finished_at = now() WHERE id = %s",
        (state, reason, job.request_id),
    )


def _mark_finished(job: Job, state: str) -> None:
    stats.outcomes[state] += 1
    timing = timings.get(job.request_id)
    if timing is not None:
        timing.finished = time.monotonic()
        _check_order(timing)
        intervals = timing.intervals_ms()
        for name in ("queue_wait_ms", "processing_ms", "total_ms"):
            stats.add(name, intervals[name])
    log("booking.finished", request_id=job.request_id, state=state)


def _check_order(t: Timing) -> None:
    marks = [m for m in (t.received, t.started, t.finished, t.first_read) if m is not None]
    if any(a > b for a, b in zip(marks, marks[1:])):
        stats.order_violations += 1


def _remember(request_id: str, timing: Timing) -> None:
    timings[request_id] = timing
    while len(timings) > TIMINGS_MAX:
        timings.popitem(last=False)


# ЛР3: задачі Ray


def _ray_connect() -> None:
    from . import ray_tasks

    while True:
        try:
            ray_tasks.connect()
            log("ray.connected", address=config.RAY_ADDRESS)
            return
        except Exception as exc:  # noqa: BLE001 - кластер ще стартує
            log("ray.connect_failed", error=type(exc).__name__)
            time.sleep(2)


async def _ray_dispatcher() -> None:
    """Подає задачі Ray, доки незавершених менше за IN_FLIGHT_LIMIT (О3.2), і обробляє ПЕРШИЙ готовий
    результат (ray.wait, num_returns=1), а не в порядку подання. Решта запитів чекає в черзі ЛР1."""
    import ray

    from . import ray_tasks

    assert queue is not None
    await asyncio.to_thread(_ray_connect)
    settings = ray_tasks.task_settings()
    pending: dict[Any, Job] = {}
    while True:
        try:
            while len(pending) < config.IN_FLIGHT_LIMIT:
                if pending:
                    try:
                        job = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                else:
                    job = await queue.get()  # незавершених задач немає: чекати роботу без опитування
                ref = await _ray_submit(job, settings)
                if ref is not None:
                    pending[ref] = job
            if not pending:
                continue
            ready, _ = await asyncio.to_thread(ray.wait, list(pending), num_returns=1, timeout=0.05)
            for ref in ready:
                await _ray_finish(pending.pop(ref), ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - диспетчер не має зупинятися
            log("ray.dispatcher_error", error=type(exc).__name__)
            await asyncio.sleep(0.5)


async def _ray_submit(job: Job, settings: dict[str, Any]) -> Any:
    from . import ray_tasks

    assert queue is not None
    await job.persisted.wait()
    if job.dropped:
        queue.task_done()
        return None
    timing = timings.get(job.request_id)
    if timing is not None:
        timing.started = time.monotonic()
    try:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE booking_request SET state = 'processing', started_at = now() WHERE id = %s", (job.request_id,)
            )
        ref = await asyncio.to_thread(
            ray_tasks.process_request.remote, job.request_id, job.doctor_day_id, job.patient_id, job.slots, settings
        )
    except Exception as exc:  # noqa: BLE001
        log("ray.submit_failed", request_id=job.request_id, error=type(exc).__name__)
        await _ray_fail(job, type(exc).__name__)
        queue.task_done()
        return None
    stats.busy += 1
    stats.submitted += 1
    stats.max_in_flight = max(stats.max_in_flight, stats.busy)
    return ref


async def _ray_finish(job: Job, ref: Any) -> None:
    import ray

    assert queue is not None
    try:
        result = await asyncio.to_thread(ray.get, ref)
        # рішення вже записав приймач; тут — лише облік часу й лічильників
        _mark_finished(job, result["decision"])
        log("ray.task_done", request_id=job.request_id, decision=result["decision"], duplicate=result["duplicate"],
            deliveries=result["deliveries"], worker=result["worker"])
    except Exception as exc:  # noqa: BLE001 - вичерпано повтори або прикладна помилка задачі
        log("ray.task_failed", request_id=job.request_id, error=type(exc).__name__)
        await _ray_fail(job, type(exc).__name__)
    finally:
        stats.busy -= 1
        queue.task_done()


async def _ray_fail(job: Job, reason: str) -> None:
    """failed — лише якщо приймач не записав рішення (ефект міг бути прийнятий до відмови)."""
    try:
        async with db.connection() as conn:
            row = await (
                await conn.execute(
                    """UPDATE booking_request SET state = 'failed', reason = %s, finished_at = now()
                       WHERE id = %s AND state NOT IN ('done', 'rejected') RETURNING id""",
                    (reason, job.request_id),
                )
            ).fetchone()
        _mark_finished(job, "failed" if row else "done")
    except Exception:  # noqa: BLE001
        log("booking.state_lost", request_id=job.request_id)
