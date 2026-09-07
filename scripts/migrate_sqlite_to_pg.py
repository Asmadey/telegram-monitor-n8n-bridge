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
Для журнала сохраняются отдельные квитанции переноса по исходному ID.

Секреты из integrations_config при переносе ОБЯЗАТЕЛЬНО шифруются:
скрипт отказывается работать без APP_ENCRYPTION_KEY, потому что
записать чужие токены открытым текстом — «задача выполнена,
а доступ открыт» (CLAUDE.md, ключевое правило).

Использование:
    python -m scripts.migrate_sqlite_to_pg --user-email owner@example.com
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import uuid
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
from app.models import (
    FeedItem,
    Integration,
    LegacyImportRow,
    LogEntry,
    Monitor,
    SentMessage,
    TelegramAccount,
    User,
)
from app.services.journal import redact

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
    conn = sqlite3.connect(Path(sqlite_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            "settings": "SELECT * FROM settings",
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
    email = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.flush()
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


def _legacy_feed_job_id(user_id, source_key) -> str:
    """Идентификатор строки ленты, выведенный из владельца и ключа источника."""
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"teleton:legacy:{user_id}:feed:{source_key}")
    )


async def _migrate_feed_items(session, rows, user_id) -> int:
    inserted = 0
    for r in rows:
        # job_id старой базы уникален ВНУТРИ неё, а колонка в Postgres —
        # глобально. Поэтому значение не переносится как есть, а всегда
        # выводится из пары (владелец, строка источника): два тенанта могут
        # перенести один и тот же экспорт, не сталкиваясь, а повторный
        # запуск остаётся идемпотентным — идентификатор детерминирован.
        job_id = _legacy_feed_job_id(user_id, r["job_id"] or r["id"])
        # photo_base64 сознательно НЕ переносится (задача 5.4 — объектное хранилище)
        stmt = (
            pg_insert(FeedItem)
            .values(
                # PK не переносим (см. sent_messages): идемпотентность —
                # по job_id, который в старой базе тоже уникален.
                user_id=user_id,
                job_id=job_id,
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
        if not result.rowcount:
            owner = await session.scalar(
                select(FeedItem.user_id).where(FeedItem.job_id == job_id)
            )
            if owner != user_id:
                raise ValueError("Legacy feed job ID belongs to another owner")
        inserted += result.rowcount or 0
    return inserted


async def _migrate_logs(session, rows, user_id) -> int:
    """Квитанции и строки журнала пишутся одной транзакцией: повторный запуск
    не удваивает их, а чужие записи назначения остаются нетронутыми."""
    inserted = 0
    for r in rows:
        receipt = await session.execute(
            pg_insert(LegacyImportRow)
            .values(user_id=user_id, source_table="logs", source_id=str(r["id"]))
            .on_conflict_do_nothing(
                index_elements=["user_id", "source_table", "source_id"]
            )
            .returning(LegacyImportRow.id)
        )
        if receipt.scalar_one_or_none() is None:
            continue
        stmt = pg_insert(LogEntry).values(
            user_id=user_id,
            timestamp=_ts_required(r["timestamp"]),
            event_type=r["event_type"],
            chat_title=r["chat_title"],
            chat_id=r["chat_id"],
            messages_count=r["messages_count"] or 0,
            status=r["status"],
            details=redact(str(r["details"] or "")),
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
                cleanup_enabled=r.get("cleanup_enabled", False),
                cleanup_days=r.get("cleanup_days", 30),
                cleanup_last_run=_ts(r.get("cleanup_last_run")),
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


def _merge_settings(rows: list[dict], settings_rows: list[dict]) -> list[dict]:
    """Значения назначения главнее: осознанно пустая настройка не
    восстанавливается устаревшей из источника."""
    settings = {r["key"]: r["value"] for r in settings_rows}
    row = dict(rows[0]) if rows else {}
    for name in (
        "telegram_bot_token",
        "telegram_forward_enabled",
        "openrouter_api_key",
        "openrouter_base_url",
        "openrouter_model",
        "openrouter_enabled",
        "webhook_url",
        "auto_webhook_enabled",
    ):
        row.setdefault(name, settings.get(name, ""))
    row.setdefault("telegram_sender_id", settings.get("telegram_forward_chat_id", ""))
    row.setdefault("updated_at", None)
    for name in (
        "telegram_forward_enabled",
        "openrouter_enabled",
        "auto_webhook_enabled",
    ):
        row[name] = str(row[name]).lower() in ("1", "true")
    row["cleanup_enabled"] = str(settings.get("auto_cleanup_enabled", "0")).lower() in (
        "1",
        "true",
    )
    row["cleanup_days"] = int(settings.get("auto_cleanup_days") or 30)
    if row["cleanup_days"] < 1:
        raise ValueError("Legacy cleanup_days must be positive")
    row["cleanup_last_run"] = settings.get("auto_cleanup_last_run")
    return [row]


async def _archive_settings(session, rows, user_id, fernet) -> int:
    count = 0
    for row in rows:
        result = await session.execute(
            pg_insert(LegacyImportRow)
            .values(
                user_id=user_id,
                source_table="settings",
                source_id=row["key"],
                payload_encrypted=fernet.encrypt(json.dumps(row).encode()).decode(),
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "source_table", "source_id"]
            )
        )
        count += result.rowcount or 0
    return count


def _read_telegram_session(path: str) -> str:
    """Прочитать сессию из SQLite офлайн и только на чтение, не создавая
    SQLiteSession: источник не изменяется."""
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1 or not rows[0][3] or len(rows[0][3]) != 256:
        raise ValueError("Legacy Telegram session has no unambiguous authorization key")
    dc_id, address, port, key = rows[0]
    converted = StringSession()
    converted.set_dc(dc_id, address, port)
    converted.auth_key = AuthKey(key)
    return converted.save()


async def _verify_telegram_session(encoded: str, session, user_id: int) -> dict:
    """Operator must stop every prior session owner before this short verification.

    Ключи приложения берутся из кабинета ВЛАДЕЛЬЦА (открытый вопрос №1,
    решён 2026-09-07): переносимая сессия создана его приложением, ими же
    её и проверяем. ENV остаётся запасным путём для оператора, у которого
    кабинет ещё пуст, — иначе перенос упирается в курицу и яйцо.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from app.services.tg_credentials import load_credentials

    pair = await load_credentials(session, user_id)
    if pair is None:
        settings = get_settings()
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise ValueError(
                "нет ключей приложения: внесите api_id/api_hash в кабинет "
                "или задайте TELEGRAM_API_ID и TELEGRAM_API_HASH оператору"
            )
        pair = (settings.telegram_api_id, settings.telegram_api_hash)
    api_id, api_hash = pair
    client = TelegramClient(StringSession(encoded), api_id, api_hash)
    try:
        await client.connect()
        me = await client.get_me()
        if me is None:
            raise ValueError("Legacy Telegram session is no longer authorized")
        return {
            "phone": me.phone or "",
            "tg_user_id": me.id,
            "tg_username": me.username,
        }
    finally:
        await client.disconnect()


async def _migrate_telegram_session(
    session, user_id, encoded, fernet, phone="", tg_user_id=0, tg_username=None
) -> int:
    result = await session.execute(
        pg_insert(TelegramAccount)
        .values(
            user_id=user_id,
            phone=phone,
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            session_string_encrypted=fernet.encrypt(encoded.encode()).decode(),
            status="active",
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    return result.rowcount or 0


async def migrate(
    sqlite_path: str,
    session,
    user_id: int,
    *,
    telegram_session: str | None = None,
    telegram_metadata: dict | None = None,
) -> dict[str, int]:
    """Переносит данные; возвращает счётчики ВСТАВЛЕННЫХ строк (не всех)."""
    fernet = _encryptor()
    if telegram_session and (
        not telegram_metadata or not telegram_metadata.get("tg_user_id")
    ):
        raise ValueError("Verified Telegram account metadata is required")
    data = _read_sqlite(sqlite_path)
    data["integrations_config"] = _merge_settings(
        data["integrations_config"], data["settings"]
    )

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
    stats["settings"] = await _archive_settings(
        session, data["settings"], user_id, fernet
    )
    if telegram_session:
        stats["telegram_accounts"] = await _migrate_telegram_session(
            session, user_id, telegram_session, fernet, **(telegram_metadata or {})
        )
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
    parser.add_argument(
        "--telegram-session-path",
        help="Legacy SQLite session; never uploaded unencrypted",
    )
    parser.add_argument(
        "--verify-telegram",
        action="store_true",
        help="Connect briefly to get_me; old worker MUST be stopped first",
    )
    args = parser.parse_args()
    if args.telegram_session_path and not args.verify_telegram:
        parser.error(
            "--telegram-session-path requires --verify-telegram after stopping the old worker"
        )
    encoded = (
        _read_telegram_session(args.telegram_session_path)
        if args.telegram_session_path
        else None
    )

    settings = get_settings()
    url = args.url or settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql"):
        sys.exit(
            "Перенос возможен только в Postgres. "
            "Задайте DATABASE_URL (Railway: ${{Postgres.DATABASE_URL}})."
        )

    _encryptor()
    _read_sqlite(args.sqlite_path)
    engine = create_async_engine(url, hide_parameters=True)
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

        await session.execute(select(1))
        # Владелец определяется ДО проверки: его ключами она и делается.
        user = await _get_or_create_user(session, args.user_email)
        metadata = (
            await _verify_telegram_session(encoded, session, user.id)
            if encoded
            else None
        )
        stats = await migrate(
            args.sqlite_path,
            session,
            user.id,
            telegram_session=encoded,
            telegram_metadata=metadata,
        )
        print(
            f"Перенесено для {user.email}: "
            + ", ".join(f"{k}={v}" for k, v in stats.items())
        )
        print(
            "Повторный запуск безопасен: дубли не создаются (ON CONFLICT DO NOTHING)."
        )

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # DB exception repr/traceback can contain plaintext legacy secrets.
        print(
            f"Migration failed ({type(exc).__name__}); transaction not confirmed. No source files changed.",
            file=sys.stderr,
        )
        sys.exit(1)
