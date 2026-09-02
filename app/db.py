"""Async-движок и get_db() (Фаза 2, целевая структура PLAN.md раздел 2).

Единственная точка создания движка: URL — только из app.config, дефолта нет
(урок инцидента 2026-09-02: дефолт «рабочей» базы в конфиге — мина).
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        url = get_settings().database_url
        if not url:
            raise RuntimeError(
                "DATABASE_URL не задан: подключаться не к чему. "
                "Дефолтной «рабочей» базы у нас нет принципиально."
            )
        _engine = create_async_engine(url)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """Роутеры получают сессию через Depends(get_db); тесты подменяют его."""
    async with get_sessionmaker()() as session:
        yield session