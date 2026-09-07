"""Сессия незавершённой попытки входа в Telegram.

Отдельный модуль, а не метод роутера: сессию попытки читает зависимость
клиента (tg_auth), а пишет роутер send-code — общий код должен лежать
там, где его видно обоим, и в одном месте решать, что считать «текущей»
попыткой.
"""

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TgAuthAttempt
from app.security.crypto import decrypt


async def current_attempt(db: AsyncSession, user_id: int) -> TgAuthAttempt | None:
    """Последняя НЕ истёкшая попытка пользователя."""
    attempt = (
        await db.scalars(
            select(TgAuthAttempt)
            .where(TgAuthAttempt.user_id == user_id)
            .order_by(TgAuthAttempt.id.desc())
        )
    ).first()
    if attempt is None:
        return None
    expires = attempt.expires_at
    if expires.tzinfo is None:  # SQLite отдаёт время без зоны
        expires = expires.replace(tzinfo=datetime.timezone.utc)
    if expires < datetime.datetime.now(datetime.timezone.utc):
        return None
    return attempt


async def attempt_session(db: AsyncSession, user_id: int) -> str:
    """Строка сессии текущей попытки; пустая — начать вход заново."""
    attempt = await current_attempt(db, user_id)
    if attempt is None or not attempt.session_string_encrypted:
        return ""
    return decrypt(attempt.session_string_encrypted)
