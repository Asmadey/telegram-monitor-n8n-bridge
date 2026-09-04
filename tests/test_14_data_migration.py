"""Задача 1.5 — перенос данных из SQLite в Postgres без потери дедупликации.

Самое важное — sent_messages. Если не перенести историю отправленных ID,
первый же опрос после переезда отправит в n8n все старые посты как новые.
Контракт: перенос идемпотентен, привязывает записи к указанному пользователю
и сохраняет работоспособность дедупликации.

Об источнике данных. До 2026-09-04 тест читал `storage.db` — живую базу
оператора — и сверял счётчики 5/192/8/308 из инвентаризации PLAN.md. Такой
тест зелёный ровно на одной машине: файл в .gitignore, в чекауте CI его нет
(первый прогон с живым Postgres упал на `no such table: monitors`), а счётчики
меняются при каждом опросе каналов. Здесь источник строится синтетически, по
старой схеме из server.py, с заведомо известным содержимым. Проверка на
настоящей базе оператора осталась отдельным тестом ниже и честно пропускается
там, где базы нет.

Поведенческий уровень: без живого Postgres проверка не выполняется —
честный skip (AGENTS.md §4), а не зелёный прогон вхолостую.
"""

import os
import sqlite3

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="нет TEST_DATABASE_URL с живым Postgres — поведенческий тест пропускается",
)

# Содержимое синтетического источника. Числа маленькие и точные: тест обязан
# ловить потерю строки, а не «примерно столько же».
N_MONITORS = 2
N_SENT = 5
N_FEED = 3
N_LOGS = 4

# Старая схема — дословно из server.py init_db() (включая колонки, доехавшие
# блоками ALTER TABLE: reactions_count, forwards, has_media, reactions_json).
LEGACY_SCHEMA = """
CREATE TABLE monitors (
    id TEXT PRIMARY KEY, chat_target TEXT NOT NULL, chat_title TEXT,
    chat_username TEXT, chat_id INTEGER, interval_minutes INTEGER DEFAULT 60,
    limit_count INTEGER DEFAULT 20, offset_hours INTEGER DEFAULT 24,
    is_active INTEGER DEFAULT 1, last_checked TEXT,
    last_sent_message_id INTEGER DEFAULT 0, prompt TEXT, created_at TEXT
);
CREATE TABLE sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, date TEXT, sender TEXT, text TEXT,
    views INTEGER, post_url TEXT, sent_at TEXT,
    reactions_count INTEGER DEFAULT 0, forwards INTEGER DEFAULT 0,
    has_media INTEGER DEFAULT 0, reactions_json TEXT DEFAULT '[]',
    UNIQUE(chat_id, message_id)
);
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL, chat_title TEXT, chat_id INTEGER,
    messages_count INTEGER DEFAULT 0, status TEXT NOT NULL, details TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE integrations_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    telegram_bot_token TEXT DEFAULT '', telegram_sender_id TEXT DEFAULT '',
    telegram_forward_enabled INTEGER DEFAULT 0,
    openrouter_api_key TEXT DEFAULT '',
    openrouter_base_url TEXT DEFAULT 'https://openrouter.ai/api/v1',
    openrouter_model TEXT DEFAULT 'deepseek/deepseek-v4-flash',
    openrouter_enabled INTEGER DEFAULT 0, webhook_url TEXT DEFAULT '',
    auto_webhook_enabled INTEGER DEFAULT 1, updated_at TEXT
);
CREATE TABLE analysis_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE,
    created_at TEXT NOT NULL, chat_id INTEGER, chat_title TEXT,
    chat_username TEXT, photo_base64 TEXT, messages_count INTEGER DEFAULT 0,
    ai_analysis TEXT, raw_messages_json TEXT, model_name TEXT,
    delivery_status TEXT
);
"""

# Секрет в источнике — чтобы проверить, что перенос кладёт его зашифрованным.
LEGACY_WEBHOOK = "https://n8n.example.com/webhook/abcd-1234"

KNOWN_CHAT_ID = -1001143063102
KNOWN_MESSAGE_IDS = [38111, 38112, 38113, 38114, 38115]


@pytest.fixture
def legacy_sqlite(tmp_path) -> str:
    """Источник по старой схеме с заведомо известным содержимым."""
    path = tmp_path / "legacy_storage.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)

    for i in range(N_MONITORS):
        conn.execute(
            "INSERT INTO monitors (id, chat_target, chat_title, chat_username,"
            " chat_id, interval_minutes, limit_count, offset_hours, is_active,"
            " last_checked, last_sent_message_id, prompt, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"mon{i}",
                f"@channel{i}",
                f"Канал {i}",
                f"channel{i}",
                KNOWN_CHAT_ID - i,
                60,
                20,
                24,
                1,
                "2026-08-29T12:00:00+00:00",
                38100 + i,
                None,
                "2026-08-01T10:00:00+00:00",
            ),
        )

    for mid in KNOWN_MESSAGE_IDS:
        conn.execute(
            "INSERT INTO sent_messages (chat_id, message_id, date, sender, text,"
            " views, post_url, sent_at, reactions_count, forwards, has_media,"
            " reactions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                KNOWN_CHAT_ID,
                mid,
                "2026-08-29T12:00:00+00:00",
                "Канал 0",
                f"пост {mid}",
                100,
                f"https://t.me/channel0/{mid}",
                "2026-08-29T12:05:00+00:00",
                3,
                1,
                0,
                "[]",
            ),
        )

    for i in range(N_FEED):
        conn.execute(
            "INSERT INTO analysis_feed (job_id, created_at, chat_id, chat_title,"
            " chat_username, photo_base64, messages_count, ai_analysis,"
            " raw_messages_json, model_name, delivery_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"job{i}",
                "2026-08-29T13:00:00+00:00",
                KNOWN_CHAT_ID,
                "Канал 0",
                "channel0",
                None,
                1,
                "сводка",
                "[]",
                "deepseek/deepseek-v4-flash",
                "SUCCESS",
            ),
        )

    for i in range(N_LOGS):
        conn.execute(
            "INSERT INTO logs (timestamp, event_type, chat_title, chat_id,"
            " messages_count, status, details) VALUES (?,?,?,?,?,?,?)",
            (
                "2026-08-29T13:00:00+00:00",
                "WEBHOOK_SENT",
                "Канал 0",
                KNOWN_CHAT_ID,
                1,
                "SUCCESS",
                "отправлено",
            ),
        )

    conn.execute(
        "INSERT INTO integrations_config (id, webhook_url, auto_webhook_enabled,"
        " openrouter_model) VALUES (1, ?, 1, 'deepseek/deepseek-v4-flash')",
        (LEGACY_WEBHOOK,),
    )
    conn.commit()
    conn.close()
    return str(path)


async def _create_user(session, email: str) -> int:
    from app.models import User

    user = User(email=email.lower())
    session.add(user)
    await session.commit()
    return user.id


def _pg_session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _upgrade_head() -> None:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")


@pytest.mark.asyncio
async def test_migration_preserves_counts_and_dedup(legacy_sqlite, alembic_target_db):
    from sqlalchemy import select

    from app.models import SentMessage
    from scripts.migrate_sqlite_to_pg import migrate

    engine, Session = _pg_session()
    _upgrade_head()

    try:
        async with Session() as session:
            user_id = await _create_user(session, "owner@example.com")
            stats = await migrate(
                sqlite_path=legacy_sqlite, session=session, user_id=user_id
            )
            await session.commit()

            assert stats["monitors"] == N_MONITORS
            assert stats["sent_messages"] == N_SENT
            assert stats["feed_items"] == N_FEED
            assert stats["logs"] == N_LOGS

            # Главное: дедупликация переехала. Без этих строк первый же опрос
            # отправит в n8n все старые посты как новые.
            moved = (
                await session.scalars(
                    select(SentMessage.message_id).where(
                        SentMessage.user_id == user_id,
                        SentMessage.chat_id == KNOWN_CHAT_ID,
                    )
                )
            ).all()
            assert sorted(moved) == KNOWN_MESSAGE_IDS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_encrypts_secrets_it_carries(legacy_sqlite, alembic_target_db):
    """Секрет из старой базы не должен лечь в Postgres открытым текстом."""
    from sqlalchemy import select

    from app.models import Integration
    from scripts.migrate_sqlite_to_pg import migrate

    engine, Session = _pg_session()
    _upgrade_head()

    try:
        async with Session() as session:
            user_id = await _create_user(session, "secrets@example.com")
            await migrate(sqlite_path=legacy_sqlite, session=session, user_id=user_id)
            await session.commit()

            stored = await session.scalar(
                select(Integration.webhook_url_encrypted).where(
                    Integration.user_id == user_id
                )
            )
            assert stored, "интеграции не перенесены"
            assert LEGACY_WEBHOOK not in stored, (
                "webhook_url лёг в Postgres открытым текстом"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_is_idempotent(legacy_sqlite, alembic_target_db):
    """Повторный прогон не создаёт дублей: оператор может запустить дважды."""
    from scripts.migrate_sqlite_to_pg import migrate

    engine, Session = _pg_session()
    _upgrade_head()

    try:
        async with Session() as session:
            user_id = await _create_user(session, "twice@example.com")
            first = await migrate(
                sqlite_path=legacy_sqlite, session=session, user_id=user_id
            )
            await session.commit()
            second = await migrate(
                sqlite_path=legacy_sqlite, session=session, user_id=user_id
            )
            await session.commit()

            assert sum(first.values()) > 0, "первый прогон ничего не перенёс"
            assert sum(second.values()) == 0, f"повторный прогон создал дубли: {second}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_operator_database_migrates(alembic_target_db):
    """Проверка на настоящей базе оператора, если она под рукой.

    `storage.db` в .gitignore — в CI его нет, и это не повод падать.
    Счётчики не фиксируем: они меняются при каждом опросе каналов; здесь
    важно, что перенос отрабатывает на реальных данных без ошибок.
    """
    from pathlib import Path

    real = Path(__file__).resolve().parents[1] / "storage.db"
    if not real.exists() or real.stat().st_size == 0:
        pytest.skip("storage.db оператора недоступен (gitignore)")

    from scripts.migrate_sqlite_to_pg import migrate

    engine, Session = _pg_session()
    _upgrade_head()
    try:
        async with Session() as session:
            user_id = await _create_user(session, "real@example.com")
            stats = await migrate(
                sqlite_path=str(real), session=session, user_id=user_id
            )
            await session.commit()
            assert sum(stats.values()) > 0, "из реальной базы не перенеслось ничего"
    finally:
        await engine.dispose()
