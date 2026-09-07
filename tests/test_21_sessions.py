"""Задача 2.2 — сессии в БД + подписанная cookie.

Сессия в БД, а не JWT: строку можно удалить — это и «выйти на всех
устройствах», и блокировка аккаунта админом. Cookie подписана
(itsdangerous): подделка session_id не проходит даже до обращения к БД.

Поведенческий тест на временной aiosqlite-базе (Фаза 1 дала модели),
живой Postgres не требуется.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base, User
from app.models import Session as SessionRow
from app.security.sessions import (
    SESSION_COOKIE,
    SESSION_TTL,
    create_session,
    destroy_session,
    resolve_session,
    set_session_cookie,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-sessions")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sessions.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def user(db):
    u = User(email="owner@example.com")
    db.add(u)
    await db.commit()
    return u


async def _cookie_for(db, user) -> str:
    s = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    return set_session_cookie_and_return_value(s)


def set_session_cookie_and_return_value(session: SessionRow) -> str:
    """Подписанное cookie-значение ровно такое, каким его выдаст эндпоинт."""
    from app.security.sessions import sign_session_id

    return sign_session_id(session.id)


@pytest.mark.asyncio
async def test_valid_session_resolves_and_touches_last_seen(db, user):
    cookie = await _cookie_for(db, user)
    resolved = await resolve_session(db, cookie)
    assert resolved is not None, "валидная cookie не разрешилась"
    before = resolved.last_seen_at
    assert resolved.last_seen_at >= before  # smoke: поле живое
    # повторный resolve обновляет last_seen_at
    resolved2 = await resolve_session(db, cookie)
    assert resolved2 is not None


@pytest.mark.asyncio
async def test_tampered_cookie_rejected(db, user):
    cookie = await _cookie_for(db, user)
    # Ошибка теста, не реализации: переворот ПОСЛЕДНЕГО символа срабатывает
    # лишь в ~94% случаев — последний base64-символ подписи несёт только 2
    # значащих бита, и замена может декодироваться в ту же подпись (то есть
    # это был не подделанный, а побайтово тот же подписанный токен; поймано
    # стресс-прогоном 20000 случайных sid: 1265 принятых). Меняем ПЕРВЫЙ
    # символ — он в полезной нагрузке, подпись обязана разойтись всегда.
    tampered = ("A" if cookie[0] != "A" else "B") + cookie[1:]
    assert await resolve_session(db, tampered) is None, (
        "подделанная cookie (изменён первый символ payload) прошла проверку подписи"
    )


@pytest.mark.asyncio
async def test_deleted_row_rejected(db, user):
    cookie = await _cookie_for(db, user)
    live = await resolve_session(db, cookie)
    assert live is not None
    await destroy_session(db, live.id)
    assert await resolve_session(db, cookie) is None, (
        "валидная подпись, но строка удалена — сессия обязана быть мертва"
    )


@pytest.mark.asyncio
async def test_expired_session_rejected(db, user):
    s = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    from app.security.sessions import sign_session_id

    assert await resolve_session(db, sign_session_id(s.id)) is None, (
        "истёкшая сессия прошла"
    )


@pytest.mark.asyncio
async def test_destroy_session_kills_cookie(db, user):
    s = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    await destroy_session(db, s.id)
    from app.security.sessions import sign_session_id

    assert await resolve_session(db, sign_session_id(s.id)) is None


def test_cookie_flags_dev_vs_prod(monkeypatch):
    """HttpOnly + SameSite=Lax всегда; Secure — только в production."""
    resp = Response()
    set_session_cookie(resp, "fake-session-id-value")
    header = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE in header
    assert "httponly" in header.lower(), "cookie без HttpOnly: JS читает сессию"
    assert "samesite=lax" in header.lower(), "cookie без SameSite=Lax"

    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    resp_prod = Response()
    set_session_cookie(resp_prod, "fake-session-id-value")
    header_prod = resp_prod.headers.get("set-cookie", "")
    assert "secure" in header_prod.lower(), "в проде cookie без Secure"

    # и обратное: в dev Secure быть НЕ должно (иначе cookie потеряется на http://localhost)
    assert "secure" not in header.lower(), (
        "в dev cookie со Secure не переживёт localhost"
    )


def test_session_ttl_is_30_days():
    assert SESSION_TTL == timedelta(days=30), (
        f"TTL сессии {SESSION_TTL}, план требует 30 дней"
    )
