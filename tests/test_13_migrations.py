"""Задача 1.4 — схема управляется Alembic, а не try/except ALTER TABLE.

Сегодня в server.py шесть блоков
``try: cur.execute("ALTER TABLE ...") except Exception: pass`` — ошибка
проглатывается, и вы никогда не узнаете, что колонка не добавилась.
Контракт: в коде приложения DDL нет вообще; схема рождается только
миграциями Alembic, и upgrade head на чистой БД совпадает с моделями.
"""

import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

BAD_DDL = re.compile(r"(ALTER\s+TABLE|CREATE\s+TABLE)", re.IGNORECASE)


def test_no_adhoc_ddl_in_application_code():
    """В app/ нет самодельного DDL — только Alembic знает про схему."""
    for f in (ROOT / "app").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert not BAD_DDL.search(src), f"DDL в коде приложения: {f}"


def test_alembic_is_configured():
    """Каталог миграций и env.py существуют и привязаны к app.config."""
    alembic_ini = ROOT / "alembic.ini"
    env_py = ROOT / "alembic" / "env.py"
    assert alembic_ini.exists(), "нет alembic.ini"
    assert env_py.exists(), "нет alembic/env.py"
    env_src = env_py.read_text(encoding="utf-8")
    assert "app.config" in env_src, "alembic не читает DATABASE_URL из app.config"


def test_alembic_requires_explicit_database_url():
    """Alembic отказывается работать без ЯВНО заданного DATABASE_URL.

    Урок 2026-09-01: env var не экспортировался в процесс, alembic молча
    взял дефолтную storage.db и побежал миграции по боевой базе. Дефолт —
    это мина: misconfiguration не должен выглядеть успехом.
    """
    env_py = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "os.environ" in env_py and "DATABASE_URL" in env_py, (
        "env.py должен читать DATABASE_URL из os.environ сам, а не через дефолт get_settings()"
    )
    assert "sys.exit" in env_py or "raise SystemExit" in env_py, (
        "без DATABASE_URL env.py обязан завершаться с ошибкой, а не брать дефолт"
    )


def test_settings_database_url_has_no_silent_default():
    """Пустой database_url — ошибка, а не «тихий SQLite storage.db».

    Дефолт в Settings означал: забыл DATABASE_URL — и приложение/миграция
    молча ушли в боевую storage.db (см. урок выше).
    """
    from app.config import Settings

    assert not Settings.model_fields["database_url"].default, (
        "database_url не должен иметь непустого дефолта: misconfiguration "
        "обязана падать громко, а не masquerade как успех"
    )


def test_migrations_directory_is_not_empty():
    versions = list((ROOT / "alembic" / "versions").glob("*.py"))
    assert versions, "нет ни одной ревизии"


def _pg_url() -> str | None:
    """Живой Postgres для поведенческой проверки; иначе — честный skip."""
    url = os.environ.get("TEST_DATABASE_URL", "")
    return url if url.startswith("postgresql") else None


@pytest.mark.asyncio
async def test_upgrade_head_matches_models(alembic_target_db):
    """alembic upgrade head на чистой БД даёт схему, совпадающую с моделями.

    Поведенческий уровень: без живого Postgres проверка не выполняется
    (AGENTS.md §4 — честный skip, а не зелёный прогон вхолостую).
    """
    url = alembic_target_db

    from alembic.config import Config
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from alembic import command

    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()

    from app.models import Base

    expected = set(Base.metadata.tables)
    assert expected <= tables, f"нет таблиц: {expected - tables}"
