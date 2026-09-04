"""Alembic-окружение Teleton (задача 1.4 PLAN.md).

Особенности:
- движок асинхронный (asyncpg/aiosqlite) — run_sync внутри connect;
- URL читается из app.config (переменные окружения / .env), а не из alembic.ini:
  в ini остаётся заглушка, чтобы секрет не утекал в конфиг;
- target_metadata = Base.metadata — автогенерация сравнивает с моделями app.models.
"""

import asyncio
import os
import sys
import threading
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Корень репозитория — АБСОЛЮТНЫМ путём и только если его там нет:
# относительный "." создал бы вторую копию пакета app (см. alembic.ini).
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.models import Base

# alembic.ini доступен только при запуске из CLI; при программном — не обязателен
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL обязан быть задан ЯВНО (os.environ). Урок 2026-09-01:
# неэкспортированная переменная + дефолт «sqlite storage.db» в app.config
# отправили миграции молча по боевой базе. Дефолта здесь нет и не будет:
# misconfiguration обязана падать громко.
_url = os.environ.get("DATABASE_URL", "").strip()
if not _url:
    sys.exit(
        "DATABASE_URL не задан. Alembic принципиально не работает по дефолту: "
        "случайный прогон миграций уйдёт в боевую БД. "
        "Задайте DATABASE_URL явно (Railway: ${{Postgres.DATABASE_URL}})."
    )
config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline-режим: генерировать SQL, не подключаясь к БД."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Работает и из CLI, и изнутри чужого event loop (asyncio.run дважды нельзя).

    Вызов `command.upgrade()` из async-кода (например, из
    scripts/migrate_sqlite_to_pg.py или теста) попадает сюда уже с
    работающим loop — тогда миграции выполняются в отдельном потоке
    со своим loop. Движок создаётся здесь же, так что пересечения с
    сессией вызывающего кода нет.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
        return
    worker = threading.Thread(target=asyncio.run, args=(run_async_migrations(),))
    worker.start()
    worker.join()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
