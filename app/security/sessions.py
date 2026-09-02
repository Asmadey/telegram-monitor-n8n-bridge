"""Сессии в БД + подписанная cookie (задача 2.2 PLAN.md).

Порт Ruby/app/controllers/concerns/authentication.rb: сессия — строка в
БД, а не JWT, потому что её можно отозвать удалением строки («выйти на
всех устройствах», блокировка аккаунта админом). Cookie подписана
itsdangerous: подделка session_id не проходит даже до обращения к БД.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Session, User

SESSION_COOKIE = "teleton_session"
SESSION_TTL = timedelta(days=30)
_SALT = "session-cookie"


def _utc(dt: datetime) -> datetime:
    """SQLite возвращает naive-datetime; считаем его UTC (записывали мы его в UTC)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _serializer() -> URLSafeSerializer:
    secret = get_settings().secret_key
    if not secret:
        raise RuntimeError(
            "SECRET_KEY не задан: без него cookie сессии невозможно подписать. "
            "Секреты в конфиге без дефолта — misconfiguration падает громко."
        )
    return URLSafeSerializer(secret, salt=_SALT)


def sign_session_id(session_id) -> str:
    """Подписанное значение cookie: <session_id>.<подпись>."""
    return _serializer().dumps({"sid": str(session_id)})


def read_session_id(cookie_value: str) -> Optional[uuid.UUID]:
    """sid из подписанной cookie без обращения к БД (привязка CSRF, задача 2.6).

    Проверяет только подпись — жива ли строка сессии, решает resolve_session.
    """
    if not cookie_value:
        return None
    try:
        payload = _serializer().loads(cookie_value)
        return uuid.UUID(payload["sid"])
    except (BadSignature, ValueError, KeyError, TypeError):
        return None


async def create_session(
    db: AsyncSession, user: User, ip: str, user_agent: str
) -> Session:
    session = Session(
        user_id=user.id,
        ip_address=ip,
        user_agent=user_agent,
        expires_at=datetime.now(timezone.utc) + SESSION_TTL,
    )
    db.add(session)
    await db.commit()
    return session


async def resolve_session(db: AsyncSession, cookie_value: str) -> Optional[Session]:
    """Подпись → строка в БД → не истекла → обновить last_seen_at.

    Любой сбой (подделка, удалённая строка, истёкшая сессия) — один и
    тот же ответ None: 401 без объяснений, существование сессии не раскрываем.
    """
    if not cookie_value:
        return None
    try:
        payload = _serializer().loads(cookie_value)
        session_id = uuid.UUID(payload["sid"])
    except (BadSignature, ValueError, KeyError, TypeError):
        return None

    session = await db.get(Session, session_id)
    if session is None:
        return None
    if _utc(session.expires_at) < datetime.now(timezone.utc):
        await db.delete(session)
        await db.commit()
        return None

    session.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    return session


async def destroy_session(db: AsyncSession, session_id) -> None:
    session = await db.get(Session, session_id)
    if session is not None:
        await db.delete(session)
        await db.commit()


def set_session_cookie(response: Response, session_id) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_session_id(session_id),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,  # JS не читает сессию — половина XSS бесполезна
        secure=get_settings().is_production,  # в dev localhost http://
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    """Выход: удалить cookie у клиента (строку в БД удаляет destroy_session)."""
    response.delete_cookie(SESSION_COOKIE, path="/")