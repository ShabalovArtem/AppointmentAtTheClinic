"""ЛР2 (DPC): паралельне інтегрування Q(t) = amp·t²·(T − t) — людей у черзі реєстратури.

Інтеграл на [0; T] — сумарне очікування за день у людино-годинах; за формулою Літтла інтеграл,
поділений на кількість пацієнтів за день, дає середній час у черзі.

Схема (О2.1):  батьківський процес (потік uvicorn) ділить одиниці сітки на `workers` суцільних
діапазонів → запускає стільки ж процесів (multiprocessing, start_method) → кожен рахує локальну суму
свого діапазону порціями → батьківський процес збирає результати з черги й об'єднує їх.
Послідовна частина: перевірка параметрів, створення процесів, збирання й об'єднання сум, еталон.

Маршрути: POST /v1/compute (синхронно 200 або асинхронно 202), GET і DELETE /v1/compute/{id} (Д2.6),
GET /v1/histogram (Д2.5).
"""

import asyncio
import math
import multiprocessing as mp
import threading
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import config, integrate
from .auth import require_token
from .jsonlog import log
from .problems import Problem

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

_run_lock = threading.Lock()  # одночасно виконується один розрахунок: виміри не заважають один одному
_jobs: dict[str, dict[str, Any]] = {}
_JOBS_MAX = 200


class ComputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    a: float = 0.0
    b: float = 12.0
    n: int = Field(1_000_000, ge=2, le=500_000_000)
    workers: int = Field(1, ge=1, le=16)
    method: Literal["midpoint", "trapezoid", "simpson"] = "midpoint"
    order: Literal["naive", "kahan", "pairwise"] = "naive"  # порядок/спосіб сумування (Д2.4)
    mode: Literal["local", "lock", "none"] = "local"  # спільна сума (О2.4)
    chunk: int = Field(10_000, ge=1, le=10_000_000)  # одиниць у порції: між порціями — спільна сума й скасування
    start_method: Literal["forkserver", "fork", "spawn"] = Field(default_factory=lambda: config.COMPUTE_START_METHOD)
    affinity: Literal["none", "spread"] = "none"  # прив'язка частин до окремих фізичних ядер (О2.3)
    function: Literal["queue", "sin", "exp"] = "queue"
    amp: float = 0.1  # людей / год³: пік черги amp·4T³/27 ≈ 25,6 особи при T = 12
    T: float = 12.0  # тривалість робочого дня реєстратури, год
    patients: int = Field(400, ge=1)  # пацієнтів за день — для середнього часу в черзі (формула Літтла)
    richardson: bool = False  # Д2.2: додатково розрахунок із 2n і екстраполяція Річардсона
    run_async: bool = Field(False, alias="async")

    @model_validator(mode="after")
    def _check(self) -> "ComputeRequest":
        if not self.b > self.a:
            raise ValueError("b must be greater than a")
        if self.method == "simpson" and self.n % 2:
            raise ValueError("simpson needs an even n")
        return self


# --------------------------------------------------------------- розрахунок (блокувальний, у потоці)


def _max_abs_f(req: ComputeRequest) -> float:
    f = integrate.make_f(req.function, req.amp, req.T)
    points = [req.a, req.b] + [req.a + (req.b - req.a) * i / 64 for i in range(1, 64)]
    if req.function == "queue" and req.a < 2 * req.T / 3 < req.b:
        points.append(2 * req.T / 3)  # максимум Q
    return max(abs(f(x)) for x in points)


def _bounds(req: ComputeRequest) -> dict[str, float]:
    """Межа похибки методу й оцінка похибки округлення (О2.2)."""
    h = (req.b - req.a) / req.n
    width = req.b - req.a
    if req.method == "midpoint":
        method = width * h ** 2 / 24 * integrate.derivative_bound(req.function, req.amp, req.T, req.a, req.b, 2)
    elif req.method == "trapezoid":
        method = width * h ** 2 / 12 * integrate.derivative_bound(req.function, req.amp, req.T, req.a, req.b, 2)
    else:
        method = width * h ** 4 / 180 * integrate.derivative_bound(req.function, req.amp, req.T, req.a, req.b, 4)
    terms = integrate.units(req.method, req.n)
    scale = width * _max_abs_f(req)  # оцінка Σ|доданків|·h
    # послідовне сумування m доданків: |похибка| ≤ (m − 1)·ε·Σ|x_i| (+ похибки обчислення самих доданків);
    # попарне — ~ log2(m)·ε·Σ|x_i|; компенсоване — ~ 2ε·Σ|x_i|
    factor = {"naive": terms, "pairwise": req.chunk + math.log2(max(terms, 2)), "kahan": 2}[req.order]
    rounding = factor * integrate.EPS * scale
    return {"method_error_bound": method, "rounding_error_bound": rounding}


def _run(req: ComputeRequest, n: int, job: dict[str, Any] | None) -> dict[str, Any]:
    ctx = mp.get_context(req.start_method)
    total_units = integrate.units(req.method, n)
    workers = min(req.workers, total_units)
    h = (req.b - req.a) / n
    params = {"function": req.function, "amp": req.amp, "T": req.T, "method": req.method, "a": req.a, "h": h,
              "mode": req.mode, "order": req.order, "chunk": req.chunk, "affinity": req.affinity}
    shared = ctx.Value("d", 0.0) if req.mode == "lock" else ctx.RawValue("d", 0.0)  # none: БЕЗ блокування
    active = ctx.Value("i", workers)
    cancel = ctx.Event()
    out = ctx.Queue()
    if job is not None:
        job["active"], job["cancel"], job["parts"] = active, cancel, workers
        if job.get("cancel_requested"):
            cancel.set()

    t0 = time.perf_counter()
    procs = [ctx.Process(target=integrate.run_part, args=(params, k, lo, hi, out, shared, active, cancel), daemon=True)
             for k, (lo, hi) in enumerate(integrate.split(total_units, workers))]
    for p in procs:
        p.start()
    t_started = time.perf_counter()
    parts = sorted((out.get() for _ in procs), key=lambda r: r["k"])
    for p in procs:
        p.join()
    t_joined = time.perf_counter()

    cancelled = any(r["cancelled"] for r in parts)
    lost = None
    if req.mode == "local":
        if req.order == "naive":
            s = 0.0
            for r in parts:
                s += r["local"]
        else:
            all_sums = [x for r in parts for x in r["sums"]]
            s = integrate.kahan_sum(all_sums) if req.order == "kahan" else integrate.pairwise(all_sums)
    else:
        s = shared.value
        expected = 0.0
        for r in parts:
            expected += r["contributed"]
        lost = {"expected": expected * h, "shared": s * h, "lost_value": (expected - s) * h,
                "lost_share": (expected - s) / expected if expected else 0.0}
    value = s * h
    t_end = time.perf_counter()
    last_ready = max(r["t_ready"] for r in parts)
    last_done = max(r["t_done"] for r in parts)
    timing = {
        "wall_s": round(t_end - t0, 6),
        "spawn_s": round(t_started - t0, 6),  # створення процесів (Process.start)
        "start_s": round(last_ready - t0, 6),  # до моменту, коли ОСТАННЯ частина почала рахувати
        "compute_s": round(max(r["t_done"] - r["t_ready"] for r in parts), 6),  # найдовша частина
        "collect_merge_s": round(t_end - last_done, 6),  # передача результатів, join, об'єднання
        "parts": [{"k": r["k"], "units": r["hi"] - r["lo"], "portions": r["portions"],
                   "start_s": round(r["t_ready"] - t0, 6), "compute_s": round(r["t_done"] - r["t_ready"], 6)}
                  for r in parts],
    }
    return {"value": value, "cancelled": cancelled, "workers": workers, "lost_update": lost, "timing": timing,
            "joined_s": round(t_joined - t0, 6)}


def compute(req: ComputeRequest, job: dict[str, Any] | None = None) -> dict[str, Any] | None:
    with _run_lock:
        if job is not None:
            if job.get("cancel_requested"):
                return None
            job["state"] = "running"
        run = _run(req, req.n, job)
        if run["cancelled"]:
            return None
        F = integrate.antiderivative(req.function, req.amp, req.T)
        reference = F(req.b) - F(req.a)
        error = run["value"] - reference
        rel = abs(error) / abs(reference) if reference else abs(error)
        result: dict[str, Any] = {
            "value": run["value"], "reference": reference, "error": error, "relative_error": rel,
            "tolerance": config.COMPUTE_TOLERANCE, "within_tolerance": rel <= config.COMPUTE_TOLERANCE,
            **_bounds(req),
            "wall_s": run["timing"]["wall_s"], "workers": run["workers"], "timing": run["timing"],
            "params": {k: v for k, v in req.model_dump(by_alias=True).items()},
        }
        if run["lost_update"] is not None:
            result["lost_update"] = run["lost_update"]
        if req.function == "queue" and req.a == 0 and req.b == req.T:
            # формула Літтла: середній час у черзі = людино-години / пацієнтів
            result["avg_wait_min"] = run["value"] / req.patients * 60
        if req.richardson:
            p = 4 if req.method == "simpson" else 2
            run2 = _run(req, req.n * 2, job)
            if run2["cancelled"]:
                return None
            rich = (2 ** p * run2["value"] - run["value"]) / (2 ** p - 1)
            result["richardson"] = {"order": p, "value_2n": run2["value"], "error_2n": run2["value"] - reference,
                                    "value": rich, "error": rich - reference}
        log("compute.done", method=req.method, n=req.n, workers=run["workers"], mode=req.mode, order=req.order,
            start_method=req.start_method, wall_s=result["wall_s"], rel_error=rel)
        return result


# --------------------------------------------------------------- маршрути


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    active = job.get("active")
    if job["state"] in ("done", "cancelled", "failed"):
        parts_active = active.value if active is not None else 0
    else:
        parts_active = active.value if active is not None else job.get("parts", 0)
    return {"id": job["id"], "state": job["state"], "active_parts": max(0, parts_active),
            "result": job.get("result"), "error": job.get("error"), "created_at": job["created_at"],
            "finished_at": job.get("finished_at")}


def _remember(job: dict[str, Any]) -> None:
    _jobs[job["id"]] = job
    while len(_jobs) > _JOBS_MAX:
        _jobs.pop(next(iter(_jobs)))


async def _run_job(req: ComputeRequest, job: dict[str, Any]) -> None:
    try:
        result = await asyncio.to_thread(compute, req, job)
        job["result"] = result
        job["state"] = "done" if result is not None else "cancelled"
    except Exception as exc:  # noqa: BLE001
        job["state"], job["error"] = "failed", type(exc).__name__
        log("compute.failed", error=type(exc).__name__)
    job["finished_at"] = time.time()


@router.post("/compute")
async def start_compute(req: ComputeRequest, response: Response) -> dict[str, Any]:
    job = {"id": f"cmp-{uuid.uuid4().hex[:12]}", "state": "queued", "created_at": time.time(), "parts": req.workers}
    _remember(job)
    response.headers["Location"] = f"/v1/compute/{job['id']}"
    if req.run_async:
        job["task"] = asyncio.create_task(_run_job(req, job))
        response.status_code = 202
        return _job_view(job)
    await _run_job(req, job)
    if job["state"] == "failed":
        raise Problem(500, "compute-failed", "Computation failed", job.get("error"))
    return _job_view(job)


@router.get("/compute/{job_id}")
async def get_compute(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise Problem(404, "not-found", "Computation not found")
    return _job_view(job)


@router.delete("/compute/{job_id}", status_code=202)
async def cancel_compute(job_id: str) -> dict[str, Any]:
    """Д2.6: сигнал скасування; частини перевіряють його між порціями й завершуються, результату немає."""
    job = _jobs.get(job_id)
    if job is None:
        raise Problem(404, "not-found", "Computation not found")
    if job["state"] in ("queued", "running"):
        job["cancel_requested"] = True
        job["cancel_requested_at"] = time.time()
        if job.get("cancel") is not None:
            job["cancel"].set()
        if job["state"] == "queued":
            job["state"] = "cancelled"
        log("compute.cancel", id=job_id)
    return _job_view(job)


def _histogram(n: int, workers: int, seed: int, start_method: str) -> dict[str, Any]:
    with _run_lock:
        ctx = mp.get_context(start_method)
        workers = min(workers, n)
        out = ctx.Queue()
        t0 = time.perf_counter()
        procs = [ctx.Process(target=integrate.histogram_part, args=(seed, lo, hi, out), daemon=True)
                 for lo, hi in integrate.split(n, workers)]
        for p in procs:
            p.start()
        parts = [out.get() for _ in procs]
        for p in procs:
            p.join()
        merged = {b: 0 for b in integrate.LATE_BINS}  # об'єднання локальних словників наприкінці
        for r in parts:
            for b, c in r["counts"].items():
                merged[b] += c
        return {"histogram": merged, "n": n, "workers": workers, "seed": seed,
                "wall_s": round(time.perf_counter() - t0, 6), "total": sum(merged.values())}


@router.get("/histogram")
async def histogram(
    n: int = Query(1_000_000, ge=1, le=50_000_000),
    workers: int = Query(4, ge=1, le=16),
    seed: int = Query(42, ge=0, le=2 ** 31),
    start_method: Literal["forkserver", "fork", "spawn"] = Query(None),
) -> dict[str, Any]:
    """Д2.5: гістограма запізнень пацієнтів (хв) — локальні словники частин об'єднуються наприкінці."""
    return await asyncio.to_thread(_histogram, n, workers, seed, start_method or config.COMPUTE_START_METHOD)
