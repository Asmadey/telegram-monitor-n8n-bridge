#!/usr/bin/env python
"""Разовый перенос данных из SQLite в Postgres (задача 1.5 PLAN.md).

Что переносит: 5 каналов, 192 отправленных сообщения, 8 записей ленты,
308 логов, 1 строку настроек интеграций — всё привязывается к пользователю
из --user-email (создаётся, если его нет).

Самое важное — sent_messages: без истории отправленных ID первый же опрос
после переезда отправит в n8n все старые посты как новые.

Идемпотентность: повторный запуск не создаёт дублей — каждая таблица
вставляется с ON CONFLICT DO NOTHING по естественному ключу
(public_id / (user_id, chat_id, message_id) / job_id). PK старой базы
НЕ переносится: он всегда начинается с 1 и ломал второго тенанта.
У журнала естественного ключа нет — он переносится только в пустой.

Секреты из integrations_config при переносе ОБЯЗАТЕЛЬНО шифруются:
скрипт отказывается работать без APP_ENCRYPTION_KEY, потому что
записать чужие токены открытым текстом — «задача выполнена,
а доступ открыт» (CLAUDE.md, ключевое правило).

Использование:
    python -m scripts.migrate_sqlite_to_pg --user-email owner@example.com
"""

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Вставляем корень в sys.path только если его там нет. Безусловная вставка
# создаёт ВТОРУЮ копию пакета app при импорте скрипта из уже настроенного
# окружения: у app.config появляется свой lru_cache, и настройки, заданные
# тестом первому экземпляру, второй не видит. В CI это проявилось как
# «письмо сброса не дошло» в совершенно другом тесте — искать пришлось долго.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import FeedItem, Integration, LogEntry, Monitor, SentMessage, User

SECRETS_PLAINTEXT_FIELDS = [
    "telegram_bot_token",
    "openrouter_api_key",
    "webhook_url",
]


def _encryptor() -> Fernet:
    """Fernet из APP_ENCRYPTION_KEY; без ключа отказываемся работать."""
    key = get_settings().app_encryption_key
    if not key:
        sys.exit(
            "APP_ENCRYPTION_KEY не задан. Перенос секретов открытым текстом "
            "запрещён — задайте ключ (openssl rand -base64 32) и повторите."
        )
    return Fernet(key)


def _read_sqlite(sqlite_path: str) -> dict[str, list[dict]]:
    """Читает все переносимые таблицы в dict-ы (sync — файл локальный)."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            "monitors": "SELECT * FROM monitors",
            "sent_messages": "SELECT * FROM sent_messages",
            "analysis_feed": "SELECT * FROM analysis_feed",
            "logs": "SELECT * FROM logs",
            "integrations_config": "SELECT * FROM integrations_config",
        }
        return {
            name: [dict(r) for r in conn.execute(sql)] for name, sql in tables.items()
        }
    finally:
        conn.close()


async def _get_or_create_user(session, email: str) -> User:
    email = email.lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.commit()
    return user


def _ts(value):
    """SQLite хранит время текстом; NULL/пустая строка остаются NULL."""
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ts_required(value):
    """То же, но для колонок NOT NULL: пустое значение заменяется на «сейчас».

    Найдено CI 2026-09-04. Старая `init_db()` создаёт строку настроек как
    `INSERT OR IGNORE INTO integrations_config (id) VALUES (1)` — без
    `updated_at`. У любого, кто ни разу не сохранял настройки интеграций,
    там NULL, и перенос падал на NOT NULL, не перенеся вообще ничего.
    На живой базе автора значение случайно было, поэтому дефект был не виден;
    вскрыл его синтетический источник, где колонка намеренно пуста.

    Перенос не имеет права падать из-за отсутствующей отметки времени:
    данные ценнее точности метки.
    """
    from datetime import datetime, timezone

    return _ts(value) or datetime.now(timezone.utc)


async def _migrate_monitors(session, rows, user_id) -> int:
    inserted = 0
    for r in rows:
        stmt = (
            pg_insert(Monitor)
            .values(
                public_id=str(r["id"]),  # старый TEXT-UUID становится публичным id
                user_id=user_id,
                chat_target=r["chat_target"],
                chat_title=r["chat_title"],
                chat_username=r["chat_username"],
                chat_id=r["chat_id"],
                interval_minutes=r["interval_minutes"] or 60,
                limit_count=r["limit_count"] or 20,
                offset_hours=r["offset_hours"] or 24,
                is_active=bool(r["is_active"] or 0),
                last_checked=_ts(r["last_checked"]),
                last_sent_message_id=r["last_sent_message_id"] or 0,
                prompt=r["prompt"],
                created_at=_ts_required(r["created_at"]),
            )
            # цель конфликта — пара, а не один public_id: уникальность стала
            # тенантной (миграция 0005), иначе Postgres не находит индекс
            .on_conflict_do_nothing(index_elements=["user_id", "public_id"])
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


async def _migrate_sent_messages(session, rows, user_id) -> int:
    """Дедупликация переезжает первой: без неё 192 дубля в n8n."""
    inserted = 0
    for r in rows:
        stmt = (
            pg_insert(SentMessage)
            .values(
                # PK старой базы НЕ переносится. Он всегда начинается с 1, и
                # у второго же тенанта вызывал duplicate key на sent_messages_pkey:
                # ON CONFLICT целится в (user_id, chat_id, message_id), а не в PK,
                # поэтому коллизию первичного ключа не перехватывал. Идемпотентность
                # обеспечивает бизнес-ключ — он и есть правильная цель конфликта.
                user_id=user_id,
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                date=_ts(r["date"]),
                sender=r["sender"],
                text=r["text"],
                views=r["views"],
                post_url=r["post_url"],
                sent_at=_ts_required(r["sent_at"]),
                reactions_count=r.get("reactions_count") or 0,
                forwards=r.get("forwards") or 0,
                has_media=bool(r.get("has_media") or 0),
                reactions_json=r.get("reactions_json") or "[]",
            )
            .on_conflict_do_nothing(index_elements=["user_id", "chat_id", "message_id"])
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


async def _migrate_feed_items(session, rows, user_id) -> int:
    inserted = 0
    for r in rows:
        # photo_base64 сознательно НЕ переносится (задача 5.4 — объектное хранилище)
        stmt = (
            pg_insert(FeedItem)
            .values(
                # PK не переносим (см. sent_messages): идемпотентность —
                # по job_id, который в старой базе тоже уникален.
                user_id=user_id,
                job_id=r["job_id"],
                created_at=_ts_required(r["created_at"]),
                chat_id=r["chat_id"],
                chat_title=r["chat_title"],
                chat_username=r["chat_username"],
                messages_count=r["messages_count"] or 0,
                ai_analysis=r["ai_analysis"],
                raw_messages_json=r["raw_messages_json"],
                model_name=r["model_name"],
                delivery_status=r["delivery_status"],
            )
            .on_conflict_do_nothing(index_elements=["job_id"])
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


async def _migrate_logs(session, rows, user_id) -> int:
    """У журнала нет естественного ключа, поэтому идемпотентность — проверкой.

    Раньше повторный прогон упирался в перенесённый PK старой базы. От переноса
    PK пришлось отказаться (он ломал второго тенанта — см. _migrate_sent_messages),
    а конфликт по автогенерируемому PK не сработает никогда: он всегда новый.
    Поэтому журнал переносится только в пустой: у пользователя, которому уже
    что-то перенесли, второй прогон не должен удвоить историю.
    """
    if await session.scalar(
        select(LogEntry.id).where(LogEntry.user_id == user_id).limit(1)
    ):
        return 0

    inserted = 0
    for r in rows:
        stmt = pg_insert(LogEntry).values(
            user_id=user_id,
            timestamp=_ts_required(r["timestamp"]),
            event_type=r["event_type"],
            chat_title=r["chat_title"],
            chat_id=r["chat_id"],
            messages_count=r["messages_count"] or 0,
            status=r["status"],
            details=r["details"],
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


async def _migrate_integrations(session, rows, user_id, fernet: Fernet) -> int:
    inserted = 0
    for r in rows:

        def enc(field: str) -> str:
            value = r.get(field) or ""
            return fernet.encrypt(value.encode()).decode() if value else ""

        stmt = (
            pg_insert(Integration)
            .values(
                user_id=user_id,
                telegram_bot_token_encrypted=enc("telegram_bot_token"),
                telegram_sender_id=r["telegram_sender_id"] or "",
                telegram_forward_enabled=bool(r["telegram_forward_enabled"] or 0),
                openrouter_api_key_encrypted=enc("openrouter_api_key"),
                openrouter_base_url=r["openrouter_base_url"]
                or "https://openrouter.ai/api/v1",
                openrouter_model=r["openrouter_model"] or "deepseek/deepseek-v4-flash",
                openrouter_enabled=bool(r["openrouter_enabled"] or 0),
                webhook_url_encrypted=enc("webhook_url"),
                auto_webhook_enabled=bool(r["auto_webhook_enabled"] or 0),
                updated_at=_ts_required(r["updated_at"]),
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


async def migrate(sqlite_path: str, session, user_id: int) -> dict[str, int]:
    """Переносит данные; возвращает счётчики ВСТАВЛЕННЫХ строк (не всех)."""
    fernet = _encryptor()
    data = _read_sqlite(sqlite_path)

    stats = {
        "monitors": await _migrate_monitors(session, data["monitors"], user_id),
        "sent_messages": await _migrate_sent_messages(
            session, data["sent_messages"], user_id
        ),
        "feed_items": await _migrate_feed_items(
            session, data["analysis_feed"], user_id
        ),
        "logs": await _migrate_logs(session, data["logs"], user_id),
        "integrations": await _migrate_integrations(
            session, data["integrations_config"], user_id, fernet
        ),
    }
    await session.commit()
    return stats


async def filter_new(session, user_id: int, chat_id: int, messages: list) -> list:
    """Из списка сообщений оставляет только те, что ещё НЕ отправлялись.

    Это и есть проверка, что дедупликация пережила переезд: старый
    message_id из sent_messages не считается новым.
    """
    ids = [m["id"] for m in messages]
    if not ids:
        return []
    known = set(
        await session.scalars(
            select(SentMessage.message_id).where(
                SentMessage.user_id == user_id,
                SentMessage.chat_id == chat_id,
                SentMessage.message_id.in_(ids),
            )
        )
    )
    return [m for m in messages if m["id"] not in known]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-email", required=True, help="владелец переносимых данных"
    )
    parser.add_argument(
        "--sqlite-path", default=str(ROOT / "storage.db"), help="путь к старой SQLite"
    )
    parser.add_argument(
        "--url", default=None, help="DATABASE_URL (по умолчанию из app.config)"
    )
    args = parser.parse_args()

    settings = get_settings()
    url = args.url or settings.database_url
    if not url.startswith("postgresql"):
        sys.exit(
            f"Перенос возможен только в Postgres, а database_url = {url!r}. "
            "Задайте DATABASE_URL (Railway: ${{Postgres.DATABASE_URL}})."
        )

    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        # Схема должна существовать до переноса: Alembic, не самодельный DDL.
        # env.py принципиально требует DATABASE_URL в os.environ (без дефолтов),
        # поэтому передаём выбранный URL через окружение — в т.ч. для --url.
        import os as _os

        from alembic.config import Config

        from alembic import command

        _os.environ["DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

        user = await _get_or_create_user(session, args.user_email)
        stats = await migrate(args.sqlite_path, session, user.id)
        print(
            f"Перенесено для {user.email}: "
            + ", ".join(f"{k}={v}" for k, v in stats.items())
        )
        print(
            "Повторный запуск безопасен: дубли не создаются (ON CONFLICT DO NOTHING)."
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
