"""C02 «Запис до клініки», частина Web, ЛР1: каталог лікарів, головна операція (утримання слотів)
і список власних записів із курсором.

Користувач — токен входу (auth.py) або до ЛР3 заголовок X-User-Id. Для пацієнта його id — це й
спільний з DPC patient_id.

Головна операція POST /v1/bookings в ОДНІЙ транзакції:
  take_slots(doctor_day_id, slots)  — спільне з кол-центром DPC правило: блокування рядка doctor_day,
                                      перевірка (день існує, не закритий, вистачає слотів) і зміна slots_taken;
  INSERT booking (held, expires_at)  — головна сутність; унікальний індекс не дає другого активного
                                      запису пацієнта до того самого лікаря на той самий день.
Відповідь 201 надсилається лише після виходу з `conn.transaction()`, тобто після COMMIT.
"""

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, db
from .auth import Staff, User
from .jsonlog import log
from .problems import Problem

router = APIRouter(prefix="/v1")

ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


# Поточний користувач: токен входу або (до ЛР3, USER_HEADER=on) заголовок X-User-Id — див. auth.py.


# ---------------------------------------------------------------- catalog


@router.get("/doctors")
async def list_doctors() -> dict[str, Any]:
    async with db.connection() as conn:
        rows = await (
            await conn.execute("SELECT id, full_name, specialty FROM doctor ORDER BY full_name, id")
        ).fetchall()
    return {"items": rows}


@router.get("/doctors/{doctor_id}/days")
async def list_doctor_days(doctor_id: str) -> dict[str, Any]:
    """Дні прийому лікаря (відкриті) з кількістю вільних слотів і ціною слоту."""
    async with db.connection() as conn:
        doctor = await (
            await conn.execute("SELECT id, full_name, specialty FROM doctor WHERE id = %s", (doctor_id,))
        ).fetchone()
        if doctor is None:
            raise Problem(404, "doctor-not-found", "Doctor not found", f"doctor_id={doctor_id}")
        rows = await (
            await conn.execute(
                """SELECT id, day, clinic_tz, slots_total, slots_taken, price_minor, currency
                   FROM doctor_day WHERE doctor_id = %s AND NOT closed AND NOT unprotected_test
                   ORDER BY day, id""",
                (doctor_id,),
            )
        ).fetchall()
    return {
        "doctor": doctor,
        "items": [
            {
                "id": r["id"],
                "day": r["day"].isoformat(),
                "clinic_tz": r["clinic_tz"],
                "slots_total": r["slots_total"],
                "slots_free": max(0, r["slots_total"] - r["slots_taken"]),
                "slot_minutes": 15,
                "price": {"amount_minor": r["price_minor"], "currency": r["currency"].strip()},
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- bookings


class BookingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # невідомі поля (status, patient_id, ...) -> 422

    doctor_day_id: str = Field(pattern=ID_PATTERN)
    slots: int = Field(ge=1, le=2)
    visit_reason: str | None = Field(default=None, max_length=500)


_BOOKING_SELECT = """
    SELECT b.id, b.channel, b.patient_id, b.doctor_day_id, b.slots, b.visit_reason, b.status, b.version,
           b.created_at, b.expires_at, b.cancelled_at, b.cancel_reason, d.day, d.clinic_tz, d.price_minor, d.currency,
           doc.id AS doctor_id, doc.full_name AS doctor_name, doc.specialty
    FROM booking b
    JOIN doctor_day d ON d.id = b.doctor_day_id
    JOIN doctor doc ON doc.id = d.doctor_id
"""


def _booking_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "status": r["status"],
        "channel": r["channel"],
        "patient_id": r["patient_id"],
        "doctor_day_id": r["doctor_day_id"],
        "doctor": {"id": r["doctor_id"], "full_name": r["doctor_name"], "specialty": r["specialty"]},
        "day": r["day"].isoformat(),
        "clinic_tz": r["clinic_tz"],
        "slots": r["slots"],
        "visit_reason": r["visit_reason"],
        # вартість: ціна слоту x кількість слотів, цілі копійки
        "price": {"amount_minor": r["price_minor"] * r["slots"], "currency": r["currency"].strip()},
        "version": str(r["version"]),
        "created_at": r["created_at"].isoformat(),
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "cancelled_at": r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
        "cancel_reason": r["cancel_reason"],
    }


_REFUSALS = {
    "doctor-day-not-found": (404, "Doctor day not found"),
    "day-closed": (409, "Doctor day is closed for booking"),
    "no-free-slots": (409, "Not enough free slots"),
}


@router.post("/bookings", status_code=201)
async def create_booking(body: BookingCreate, user: User, response: Response) -> dict[str, Any]:
    booking_id = f"bk-{uuid.uuid4().hex[:20]}"
    try:
        async with db.connection() as conn, conn.transaction():
            reason = (
                await (
                    await conn.execute("SELECT take_slots(%s, %s) AS reason", (body.doctor_day_id, body.slots))
                ).fetchone()
            )["reason"]
            if reason is not None:
                status, title = _REFUSALS[reason]
                raise Problem(status, reason, title, f"doctor_day_id={body.doctor_day_id}, slots={body.slots}")
            await conn.execute(
                """INSERT INTO booking (id, channel, patient_id, doctor_day_id, slots, visit_reason, status, expires_at)
                   VALUES (%s, 'web', %s, %s, %s, %s, 'held', now() + make_interval(mins => %s))""",
                (booking_id, user["id"], body.doctor_day_id, body.slots, body.visit_reason, config.HOLD_MINUTES),
            )
            row = await (await conn.execute(_BOOKING_SELECT + " WHERE b.id = %s", (booking_id,))).fetchone()
        # тут транзакцію вже зафіксовано
    except psycopg.errors.UniqueViolation:
        raise Problem(
            409,
            "already-booked",
            "Patient already has an active booking with this doctor on this day",
            f"doctor_day_id={body.doctor_day_id}",
        ) from None

    log("booking.created", booking_id=booking_id, doctor_day_id=body.doctor_day_id, slots=body.slots)
    response.headers["Location"] = f"/v1/bookings/{booking_id}"
    response.headers["ETag"] = f'"{row["version"]}"'
    return _booking_json(row)


def _encode_cursor(created_at: datetime, booking_id: str) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": booking_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        moment = datetime.fromisoformat(data["t"])
        booking_id = data["id"]
        if moment.tzinfo is None or not isinstance(booking_id, str):
            raise ValueError
        return moment, booking_id
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise Problem(400, "invalid-cursor", "Invalid cursor", "use next_cursor from the previous page") from None


@router.get("/bookings")
async def list_bookings(
    user: User,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    """Мої записи від найновіших; порядок і курсор — пара (created_at, id)."""
    params: list[Any] = [user["id"]]
    after = ""
    if cursor:
        moment, booking_id = _decode_cursor(cursor)
        after = " AND (b.created_at, b.id) < (%s, %s)"
        params += [moment, booking_id]
    params.append(limit + 1)
    async with db.connection() as conn:
        rows = await (
            await conn.execute(
                _BOOKING_SELECT + " WHERE b.patient_id = %s" + after + " ORDER BY b.created_at DESC, b.id DESC LIMIT %s",
                params,
            )
        ).fetchall()
    page, more = rows[:limit], len(rows) > limit
    return {
        "items": [_booking_json(r) for r in page],
        "next_cursor": _encode_cursor(page[-1]["created_at"], page[-1]["id"]) if more else None,
    }


@router.get("/bookings/{booking_id}")
async def get_booking(booking_id: str, user: User, response: Response) -> dict[str, Any]:
    """Лише власний запис; чужий або неіснуючий — однаково 404."""
    async with db.connection() as conn:
        row = await (
            await conn.execute(_BOOKING_SELECT + " WHERE b.id = %s AND b.patient_id = %s", (booking_id, user["id"]))
        ).fetchone()
    if row is None:
        raise Problem(404, "not-found", "Booking not found")
    response.headers["ETag"] = f'"{row["version"]}"'
    return _booking_json(row)


# ---------------------------------------------------------------- change, cancel, close day


class BookingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # власник, стан, версія, слоти — 422
    visit_reason: str | None = Field(default=None, max_length=500)


def _etag_version(if_match: str | None) -> int:
    if not if_match:
        raise Problem(428, "precondition-required", "If-Match is required", "send the ETag of GET /v1/bookings/{id}")
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:]
    try:
        return int(value.strip('"'))
    except ValueError:
        raise Problem(412, "precondition-failed", "If-Match does not match the current version") from None


@router.patch("/bookings/{booking_id}")
async def update_booking(
    booking_id: str,
    body: BookingUpdate,
    user: User,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Причина візиту з If-Match: умова версії й зміна — один UPDATE, тож дві зміни з тією самою
    версією не перезапишуть одна одну (друга отримає 412)."""
    version = _etag_version(if_match)
    async with db.connection() as conn, conn.transaction():
        updated = await (
            await conn.execute(
                """UPDATE booking SET visit_reason = %s, version = version + 1
                   WHERE id = %s AND patient_id = %s AND version = %s AND status IN ('held', 'confirmed')
                   RETURNING id""",
                (body.visit_reason, booking_id, user["id"], version),
            )
        ).fetchone()
        row = await (
            await conn.execute(_BOOKING_SELECT + " WHERE b.id = %s AND b.patient_id = %s", (booking_id, user["id"]))
        ).fetchone()
    if row is None:
        raise Problem(404, "not-found", "Booking not found")
    etag = f'"{row["version"]}"'
    if updated is None:
        if row["status"] not in ("held", "confirmed"):
            raise Problem(409, "booking-closed", "Booking can no longer be changed", f"status={row['status']}")
        raise Problem(412, "precondition-failed", "Booking was changed by someone else",
                      f"current version {row['version']}", headers={"ETag": etag})
    response.headers["ETag"] = etag
    return _booking_json(row)


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str, user: User) -> dict[str, Any]:
    """Скасування власного запису. Рядок блокується; слоти повертаються лише при переході
    held|confirmed -> cancelled, тому повторне скасування повертає той самий стан і слоти вдруге не віддає."""
    async with db.connection() as conn, conn.transaction():
        current = await (
            await conn.execute(
                "SELECT status, slots, doctor_day_id FROM booking WHERE id = %s AND patient_id = %s FOR UPDATE",
                (booking_id, user["id"]),
            )
        ).fetchone()
        if current is None:
            raise Problem(404, "not-found", "Booking not found")
        if current["status"] in ("held", "confirmed"):
            await conn.execute(
                """UPDATE booking SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'patient',
                          version = version + 1 WHERE id = %s""",
                (booking_id,),
            )
            await conn.execute("SELECT release_slots(%s, %s)", (current["doctor_day_id"], current["slots"]))
            log("booking.cancelled", booking_id=booking_id)
        elif current["status"] != "cancelled":
            raise Problem(409, "booking-closed", "Booking cannot be cancelled", f"status={current['status']}")
        row = await (await conn.execute(_BOOKING_SELECT + " WHERE b.id = %s", (booking_id,))).fetchone()
    return _booking_json(row)


def _day_json(r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": r["id"],
        "day": r["day"].isoformat(),
        "clinic_tz": r["clinic_tz"],
        "slots_total": r["slots_total"],
        "slots_free": max(0, r["slots_total"] - r["slots_taken"]),
        "slot_minutes": 15,
        "price": {"amount_minor": r["price_minor"], "currency": r["currency"].strip()},
        "closed": r["closed"],
    }
    if "doctor_id" in r:
        out["doctor"] = {"id": r["doctor_id"], "full_name": r["full_name"], "specialty": r["specialty"]}
    return out


_DAY_SELECT = """SELECT d.id, d.day, d.clinic_tz, d.slots_total, d.slots_taken, d.price_minor, d.currency, d.closed,
                        doc.id AS doctor_id, doc.full_name, doc.specialty
                 FROM doctor_day d JOIN doctor doc ON doc.id = d.doctor_id"""


@router.post("/doctor-days/{doctor_day_id}/close")
async def close_doctor_day(doctor_day_id: str, staff: Staff) -> dict[str, Any]:
    """Реєстратор закриває день: нові записи обох каналів отримують відмову day-closed (take_slots),
    активні записи скасовуються (cancel_reason=day-closed), їхні слоти повертаються. Повтор — той самий стан."""
    async with db.connection() as conn, conn.transaction():
        day = await (
            await conn.execute("SELECT id FROM doctor_day WHERE id = %s FOR UPDATE", (doctor_day_id,))
        ).fetchone()
        if day is None:
            raise Problem(404, "not-found", "Doctor day not found")
        cancelled = await (
            await conn.execute(
                """UPDATE booking SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'day-closed',
                          version = version + 1
                   WHERE doctor_day_id = %s AND status IN ('held', 'confirmed') RETURNING id, slots""",
                (doctor_day_id,),
            )
        ).fetchall()
        freed = sum(r["slots"] for r in cancelled)
        if freed:
            await conn.execute("SELECT release_slots(%s, %s)", (doctor_day_id, freed))
        await conn.execute("UPDATE doctor_day SET closed = true WHERE id = %s", (doctor_day_id,))
        row = await (await conn.execute(_DAY_SELECT + " WHERE d.id = %s", (doctor_day_id,))).fetchone()
    # сповіщення пацієнтам про скасування — ЛР5 (WebSocket); зараз пацієнт бачить cancel_reason у своїх записах
    log("doctor_day.closed", doctor_day_id=doctor_day_id, staff_id=staff["id"], cancelled=len(cancelled))
    return {**_day_json(row), "cancelled_bookings": [r["id"] for r in cancelled]}


@router.get("/doctor-days")
async def list_all_doctor_days(staff: Staff) -> dict[str, Any]:
    """Розклад для реєстратури: усі дні, включно із закритими (крім тестових днів дослідів)."""
    async with db.connection() as conn:
        rows = await (
            await conn.execute(_DAY_SELECT + " WHERE NOT d.unprotected_test ORDER BY d.day, doc.full_name, d.id")
        ).fetchall()
    return {"items": [_day_json(r) for r in rows]}
