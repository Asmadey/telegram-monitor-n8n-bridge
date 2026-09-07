"""Offline legacy migration contract; all source data is synthetic."""

import sqlite3

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import FeedItem, Integration, TelegramAccount, User
from scripts import migrate_sqlite_to_pg as migration


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    try:
        migration._read_sqlite(str(path))
    except (sqlite3.Error, FileNotFoundError):
        pass
    assert not path.exists(), "Migration created a missing source database"


@pytest.mark.asyncio
async def test_cleanup_settings_preserved(db_engine):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        user = User(email="migration@example.com")
        db.add(user)
        await db.flush()
        row = dict(
            telegram_bot_token="",
            telegram_sender_id="",
            telegram_forward_enabled=0,
            openrouter_api_key="",
            openrouter_base_url="https://example.org/v1",
            openrouter_model="test-model",
            openrouter_enabled=1,
            webhook_url="",
            auto_webhook_enabled=0,
            updated_at=None,
        )
        assert hasattr(migration, "_merge_settings"), "Legacy settings are not migrated"
        row = migration._merge_settings(
            [row],
            [
                {"key": "auto_cleanup_days", "value": "75"},
                {"key": "auto_cleanup_enabled", "value": "1"},
            ],
        )[0]
        await migration._migrate_integrations(
            db, [row], user.id, Fernet(Fernet.generate_key())
        )
        target = await db.scalar(select(Integration))
        assert target.cleanup_days == 75
        assert target.cleanup_enabled is True


def test_offline_session_conversion_does_not_write_source(tmp_path):
    source = tmp_path / "old.session"
    db = sqlite3.connect(source)
    db.execute(
        "CREATE TABLE sessions(dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB, takeout_id INTEGER)"
    )
    db.execute(
        "INSERT INTO sessions VALUES (2, ?, 443, ?, NULL)",
        ("149.154.167.51", bytes(range(256))),
    )
    db.commit()
    db.close()
    original = source.read_bytes()
    assert hasattr(migration, "_read_telegram_session"), (
        "Telegram session migration is missing"
    )
    encoded = migration._read_telegram_session(str(source))
    from telethon.sessions import StringSession

    decoded = StringSession(encoded)
    assert decoded.auth_key.key == bytes(range(256))
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
async def test_session_import_does_not_replace_existing(db_engine):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        user = User(email="session@example.com")
        db.add(user)
        await db.flush()
        assert hasattr(migration, "_migrate_telegram_session"), (
            "Telegram session migration is missing"
        )
        fernet = Fernet(Fernet.generate_key())
        assert (
            await migration._migrate_telegram_session(db, user.id, "first", fernet) == 1
        )
        assert (
            await migration._migrate_telegram_session(db, user.id, "second", fernet)
            == 0
        )
        row = await db.scalar(select(TelegramAccount))
        assert fernet.decrypt(row.session_string_encrypted.encode()).decode() == "first"


@pytest.mark.asyncio
async def test_log_import_preserves_existing_history_and_replays_once(db_engine):
    from datetime import datetime, timezone

    from app.models import LogEntry

    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        user = User(email="logs@example.com")
        db.add(user)
        await db.flush()
        db.add(
            LogEntry(
                user_id=user.id,
                timestamp=datetime.now(timezone.utc),
                event_type="CURRENT",
                status="SUCCESS",
            )
        )
        await db.flush()
        rows = [
            dict(
                id=i,
                timestamp="2026-09-01T00:00:00+00:00",
                event_type="LEGACY",
                chat_title=None,
                chat_id=None,
                messages_count=0,
                status="SUCCESS",
                details="same event",
            )
            for i in (1, 2)
        ]
        assert await migration._migrate_logs(db, rows, user.id) == 2
        assert await migration._migrate_logs(db, rows, user.id) == 0
        assert len((await db.scalars(select(LogEntry))).all()) == 3


def test_empty_integration_setting_does_not_restore_stale_secret():
    result = migration._merge_settings(
        [{"webhook_url": ""}],
        [{"key": "webhook_url", "value": "https://stale.example/hook"}],
    )
    assert result[0]["webhook_url"] == ""


@pytest.mark.asyncio
async def test_log_import_redacts_credentials(db_engine):
    from app.models import LogEntry

    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        user = User(email="redact@example.com")
        db.add(user)
        await db.flush()
        await migration._migrate_logs(
            db,
            [
                dict(
                    id=1,
                    timestamp=None,
                    event_type="ERROR",
                    chat_title=None,
                    chat_id=None,
                    messages_count=0,
                    status="ERROR",
                    details="request failed: Bearer synthetic-token",
                )
            ],
            user.id,
        )
        row = await db.scalar(select(LogEntry))
        assert "synthetic-token" not in row.details
        assert "request failed" in row.details


@pytest.mark.asyncio
async def test_public_migrate_requires_verified_session_metadata(db_engine, tmp_path):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        with pytest.raises(ValueError, match="Verified Telegram"):
            await migration.migrate(
                str(tmp_path / "absent.db"), db, 1, telegram_session="synthetic"
            )


@pytest.mark.asyncio
async def test_complete_legacy_import_and_repeat_preserve_counts(db_engine, tmp_path):
    """Run the real importer from a synthetic legacy file, including dedup after import."""
    import runpy
    from pathlib import Path

    from app.models import FeedItem, LegacyImportRow, LogEntry, Monitor, SentMessage

    legacy = runpy.run_path(str(Path(__file__).with_name("test_14_data_migration.py")))
    source = legacy["legacy_sqlite"].__wrapped__(tmp_path)
    db = sqlite3.connect(source)
    db.execute("INSERT INTO settings VALUES (?, ?)", ("auto_cleanup_days", "75"))
    db.commit()
    db.close()
    before = Path(source).read_bytes()
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as target:
        owner = await migration._get_or_create_user(target, "Migration@Example.com")
        first = await migration.migrate(
            source,
            target,
            owner.id,
            telegram_session="synthetic",
            telegram_metadata={"tg_user_id": 123, "phone": "123"},
        )
        second = await migration.migrate(
            source,
            target,
            owner.id,
            telegram_session="synthetic",
            telegram_metadata={"tg_user_id": 123, "phone": "123"},
        )
        assert first == {
            "monitors": 2,
            "sent_messages": 5,
            "feed_items": 3,
            "logs": 4,
            "integrations": 1,
            "settings": 1,
            "telegram_accounts": 1,
        }
        assert sum(second.values()) == 0
        for model, expected in [
            (Monitor, 2),
            (SentMessage, 5),
            (FeedItem, 3),
            (LogEntry, 4),
            (LegacyImportRow, 5),
        ]:
            assert len((await target.scalars(select(model))).all()) == expected
        assert await migration.filter_new(
            target, owner.id, legacy["KNOWN_CHAT_ID"], [{"id": 38111}, {"id": 99999}]
        ) == [{"id": 99999}]
    assert Path(source).read_bytes() == before


def _legacy_feed_row(source_id: int = 1, job_id: str = "shared-job") -> dict:
    return dict(
        id=source_id,
        job_id=job_id,
        created_at=None,
        chat_id=None,
        chat_title=None,
        chat_username=None,
        messages_count=0,
        ai_analysis="",
        raw_messages_json="[]",
        model_name="",
        delivery_status="",
    )


@pytest.mark.asyncio
async def test_two_owners_import_the_same_legacy_export(db_engine):
    """Один экспорт, перенесённый двум владельцам, не должен схлопываться.

    `job_id` уникален внутри старой базы, а колонка в Postgres уникальна
    ГЛОБАЛЬНО. Пока значение переносилось как есть, второй владелец,
    импортирующий тот же источник, натыкался на чужую строку — и защита от
    тихой потери справедливо поднимала ошибку вместо переноса. Найдено
    прогоном CI на живом Postgres: там все тесты переноса делят одну базу,
    поэтому «два владельца, один источник» получилось непреднамеренно.
    """
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        one = User(email="one-source@example.com")
        two = User(email="two-source@example.com")
        db.add_all([one, two])
        await db.flush()

        row = _legacy_feed_row()
        assert await migration._migrate_feed_items(db, [row], one.id) == 1
        assert await migration._migrate_feed_items(db, [row], two.id) == 1, (
            "второй владелец не получил свою копию строки ленты"
        )

        owners = sorted((await db.scalars(select(FeedItem.user_id))).all())
        assert owners == sorted([one.id, two.id])


@pytest.mark.asyncio
async def test_feed_collision_with_another_owner_is_not_silently_dropped(db_engine):
    from app.models import FeedItem

    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as db:
        one, two = User(email="one@example.com"), User(email="two@example.com")
        db.add_all([one, two])
        await db.flush()
        # Столкновение строится на ВЫВЕДЕННОМ идентификаторе (2026-09-07):
        # с namespacing по владельцу совпадение job_id из двух разных
        # экспортов больше не возникает, и прежняя постановка — одинаковый
        # legacy job_id у двух владельцев — проверяла бы уже невозможное.
        # Сама защита нужна: тихо пропущенная строка выглядит как успешный
        # перенос, при котором данные не приехали.
        taken = migration._legacy_feed_job_id(two.id, "same-job")
        db.add(FeedItem(user_id=one.id, job_id=taken, messages_count=0))
        await db.flush()
        row = _legacy_feed_row(job_id="same-job")
        with pytest.raises(ValueError, match="another owner"):
            await migration._migrate_feed_items(db, [row], two.id)
