"""Вхід, реєстрація пацієнта, вихід і поточна особа (C02, частина Web).

Токен — випадковий рядок (secrets.token_urlsafe, 256 біт) у заголовку `Authorization: Bearer`.
У базі (таблиця user_session) лише його SHA-256, строк дії й позначка відкликання: стан спільний
для app-a й app-b, вихід діє одразу на обох. Паролі перевіряє PostgreSQL: bcrypt з pgcrypto
(`crypt(пароль, хеш) = хеш`); пароль і токен не потрапляють у журнал.

Режим навчального етапу ЛР1–ЛР2: якщо `USER_HEADER=on`, без токена приймається заголовок
X-User-Id (так велить методичка до ЛР3). `USER_HEADER=off` вимикає його.
"""

import hashlib
import secrets
import uuid
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, db
from .jsonlog import log
from .problems import Problem

router = APIRouter(prefix="/v1")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def current_user(request: Request, x_user_id: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    token = _bearer(request)
    async with db.connection() as conn:
        if token:
            user = await (
                await conn.execute(
                    """SELECT u.id, u.login, u.name, u.role FROM user_session s JOIN app_user u ON u.id = s.user_id
                       WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
                    (_hash(token),),
                )
            ).fetchone()
            if user is None:
                raise Problem(401, "unauthorized", "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
            request.state.token = token
            return user
        if x_user_id and config.USER_HEADER:
            user = await (
                await conn.execute("SELECT id, login, name, role FROM app_user WHERE id = %s", (x_user_id,))
            ).fetchone()
            if user is not None:
                return user
    raise Problem(401, "unauthorized", "Login required", headers={"WWW-Authenticate": "Bearer"})


User = Annotated[dict[str, Any], Depends(current_user)]


async def require_staff(user: User) -> dict[str, Any]:
    if user["role"] != "staff":
        raise Problem(403, "forbidden", "Only registry staff may do this")
    return user


Staff = Annotated[dict[str, Any], Depends(require_staff)]


def _me(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "login": user["login"], "name": user["name"], "role": user["role"]}


async def _open_session(conn: psycopg.AsyncConnection, user_id: str) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    row = await (
        await conn.execute(
            """INSERT INTO user_session (token_hash, user_id, expires_at)
               VALUES (%s, %s, now() + make_interval(hours => %s)) RETURNING expires_at""",
            (_hash(token), user_id, config.SESSION_HOURS),
        )
    ).fetchone()
    return {"access_token": token, "token_type": "Bearer", "expires_at": row["expires_at"].isoformat()}


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(pattern=r"^[A-Za-z0-9._-]{3,40}$")
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(min_length=2, max_length=100)


@router.post("/auth/register", status_code=201)
async def register(body: Registration, response: Response) -> dict[str, Any]:
    """Реєстрація пацієнта: новий обліковий запис ролі patient і одразу вхід."""
    user_id = f"p-{uuid.uuid4().hex[:12]}"
    login = body.login.lower()
    try:
        async with db.connection() as conn, conn.transaction():
            user = await (
                await conn.execute(
                    """INSERT INTO app_user (id, login, name, password_hash, role)
                       VALUES (%s, %s, %s, crypt(%s, gen_salt('bf')), 'patient')
                       RETURNING id, login, name, role""",
                    (user_id, login, body.name.strip(), body.password),
                )
            ).fetchone()
            session = await _open_session(conn, user_id)
    except psycopg.errors.UniqueViolation:
        raise Problem(409, "login-taken", "Login is already taken", f"login={login}") from None
    log("auth.registered", user_id=user_id)
    response.headers["Location"] = "/v1/me"
    return {**session, "user": _me(user)}


@router.post("/auth/login")
async def login(body: Credentials) -> dict[str, Any]:
    async with db.connection() as conn, conn.transaction():
        user = await (
            await conn.execute(
                """SELECT id, login, name, role FROM app_user
                   WHERE login = %s AND password_hash = crypt(%s, password_hash)""",
                (body.login.lower(), body.password),
            )
        ).fetchone()
        if user is None:
            # одна відповідь і для невідомого логіна, і для хибного пароля
            raise Problem(401, "invalid-credentials", "Wrong login or password")
        session = await _open_session(conn, user["id"])
    log("auth.login", user_id=user["id"])
    return {**session, "user": _me(user)}


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, user: User) -> Response:
    token = getattr(request.state, "token", None)
    if token:
        async with db.connection() as conn:
            await conn.execute("UPDATE user_session SET revoked_at = now() WHERE token_hash = %s", (_hash(token),))
    log("auth.logout", user_id=user["id"])
    return Response(status_code=204)


@router.get("/me")
async def me(user: User) -> dict[str, Any]:
    return _me(user)
