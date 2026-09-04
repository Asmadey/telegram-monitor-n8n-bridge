"""Async-движок и get_db() (Фаза 2, целевая структура PLAN.md раздел 2).

Единственная точка создания движка: URL — только из app.config, дефолта нет
(урок инцидента 2026-09-02: дефолт «рабочей» базы в конфиге — мина).
"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
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


class TenantRepo:
    """Слой доступа к данным тенанта (задача 3.1 PLAN.md).

    Единственный законный способ читать/писать тенантные таблицы:
    фильтр user_id вшит сюда, писать «WHERE user_id = ...» руками
    в роутерах нельзя — один забытый фильтр и пользователь A читает
    ленту пользователя B. Получить репозиторий можно только через
    get_tenant_repo (deps) — поверх require_user, user_id всегда
    текущего юзера, подделать его нельзя.
    """

    def __init__(self, db: AsyncSession, user_id: int):
        self.db, self.user_id = db, user_id

    def query(self, model):
        return select(model).where(model.user_id == self.user_id)

    async def get(self, model, id_):
        """Строка по id — или None, если её нет ИЛИ она чужая.

        None роутер превращает в 404: «найдено, но чужое» (403)
        подтверждает существование объекта — утечка информации.
        """
        stmt = self.query(model).where(model.id == id_)
        return (await self.db.scalars(stmt)).first()


def deleted_count(result: Any) -> int:
    """Сколько строк удалил DELETE.

    SQLAlchemy типизирует `execute()` как `Result`, у которого `rowcount` нет,
    хотя DELETE возвращает `CursorResult`, у которого он есть. Один помощник
    вместо `type: ignore` в каждом месте удаления — их уже три.
    """
    return getattr(result, "rowcount", 0) or 0
