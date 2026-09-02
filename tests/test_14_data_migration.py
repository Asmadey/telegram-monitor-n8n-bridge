"""Задача 1.5 — перенос данных из SQLite в Postgres без потери дедупликации.

Самое важное — sent_messages. Если не перенести историю отправленных ID,
первый же опрос после переезда отправит в n8n все старые посты как новые
(192 дубля). Контракт: перенос идемпотентен, привязывает записи к
указанному пользователю и сохраняет работоспособность дедупликации.

Поведенческий уровень: без живого Postgres проверка не выполняется —
честный skip (AGENTS.md §4), а не зелёный прогон вхолостую.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="нет TEST_DATABASE_URL с живым Postgres — поведенческий тест пропускается",
)


async def _create_user(session, email: str) -> int:

    from app.models import User

    user = User(email=email.lower())
    session.add(user)
    await session.commit()
    return user.id


@pytest.mark.asyncio
async def test_migration_preserves_counts_and_dedup(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scripts.migrate_sqlite_to_pg import migrate

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Чистая схема: миграции Alembic обязаны отработать до переноса данных
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")

    async with Session() as session:
        user_id = await _create_user(session, "owner@example.com")
        stats = await migrate(
            sqlite_path="storage.db", session=session, user_id=user_id
        )

        # Считаем то, что реально лежит в исходной SQLite (5 каналов,
        # 192 сообщения, 8 записей ленты, 308 логов, 1 строка настроек —
        # по инвентаризации PLAN.md; здесь сверяемся с возвращённой статистикой).
        assert stats["monitors"] == 5
        assert stats["sent_messages"] == 192
        assert stats["feed_items"] == 8
        assert stats["logs"] == 308
        assert stats["integrations"] == 1

        # Каждая перенесённая строка видна только этому пользователю
        from sqlalchemy import func, select

        from app.models import LogEntry, Monitor, SentMessage

        for model, expected in [
            (Monitor, 5),
            (SentMessage, 192),
            (LogEntry, 308),
        ]:
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
            assert count == expected, f"{model.__tablename__}: перенесено {count}"

        # Дедупликация действительно работает после переезда:
        # старый message_id не считается новым.
        sample = await session.scalar(
            select(SentMessage).where(SentMessage.user_id == user_id).limit(1)
        )
        assert sample is not None, "sent_messages пуст — дедуп проверить не на чем"

        fake = [
            {"id": sample.message_id, "chat_id": sample.chat_id, "text": "старый пост"}
        ]
        from scripts.migrate_sqlite_to_pg import filter_new

        fresh = await filter_new(session, user_id, sample.chat_id, fake)
        assert fresh == [], "старый пост прошёл дедуп — будет дубль в n8n"

    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    """Повторный запуск не создаёт дублей (ON CONFLICT DO NOTHING)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scripts.migrate_sqlite_to_pg import migrate

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    Session = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")

    async with Session() as session:
        user_id = await _create_user(session, "owner2@example.com")
        first = await migrate(
            sqlite_path="storage.db", session=session, user_id=user_id
        )
        second = await migrate(
            sqlite_path="storage.db", session=session, user_id=user_id
        )
        assert second["monitors"] == 0, "повторный миграция надублировала каналы"
        assert second["sent_messages"] == 0, (
            "повторная миграция надублировала сообщения"
        )
        assert first["monitors"] == 5, "первый прогон должен перенести 5 каналов"

    await engine.dispose()
