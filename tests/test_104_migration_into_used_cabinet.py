"""Перенос в кабинет, которым уже пользовались (10.2b).

Задача 1.5 писалась под пустой кабинет: пользователь заводится, данные
переезжают, всё. Реальность 9 сентября другая — владелец успел месяц
работать в новой сборке: завёл два канала руками, поправил интервалы,
сохранил настройки интеграций. Строка `integrations` у него уже есть,
и два канала из пяти уже заведены.

В этом состоянии скрипт делает ровно не то, чего от него ждут:

- `integrations` вставляется с `ON CONFLICT (user_id) DO NOTHING` —
  существующая строка НЕ трогается, и ключ OpenRouter, токен бота и адрес
  вебхука просто не приезжают. Скрипт при этом отчитается «перенесено 0»
  и завершится успешно: молчаливый отказ ровно в том единственном, ради
  чего перенос и затевали;
- `monitors` дедуплицируются по `(user_id, public_id)`, а у заведённого
  руками канала public_id НОВЫЙ. Значит тот же самый канал приедет вторым
  экземпляром, и опрашиваться будет дважды.

Правило, которое здесь закрепляется: **перенос дополняет, а не спорит.**
Пустое в назначении заполняется из источника; заполненное в назначении —
это осознанный выбор владельца, и источник его не перебивает. Канал,
который уже есть, узнаётся по чату, а не по идентификатору строки.
"""

import pytest

pytestmark = pytest.mark.asyncio

LEGACY_KEY = "sk-" + "or-v1-" + "abcdef0123456789" * 2
LEGACY_TOKEN = "1111" + "22222:" + "AAF-" + "legacytokenlegacytokenlegacyto12"
LEGACY_HOOK = "https://n8n.example.com/webhook/legacy"


def _legacy_integration() -> dict:
    return {
        "telegram_bot_token": LEGACY_TOKEN,
        "telegram_sender_id": "555000",
        "telegram_forward_enabled": 1,
        "openrouter_api_key": LEGACY_KEY,
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_model": "google/gemini-2.0-flash-001",
        "openrouter_enabled": 1,
        "webhook_url": LEGACY_HOOK,
        "auto_webhook_enabled": 1,
        "updated_at": "2026-08-01T10:00:00",
        "cleanup_enabled": False,
        "cleanup_days": 30,
        "cleanup_last_run": None,
    }


def _legacy_monitor(public_id: str, chat_id: int, title: str) -> dict:
    return {
        "id": public_id,
        "chat_target": f"@{title}",
        "chat_title": title,
        "chat_username": title,
        "chat_id": chat_id,
        "interval_minutes": 60,
        "limit_count": 20,
        "offset_hours": 24,
        "is_active": 1,
        "last_checked": None,
        "last_sent_message_id": 0,
        "prompt": "старый промпт",
        "created_at": "2026-08-01T10:00:00",
    }


async def test_existing_integration_row_receives_the_missing_secrets(db, user):
    """Строка настроек уже есть — секреты обязаны в неё приехать."""
    from cryptography.fernet import Fernet

    from app.config import get_settings
    from app.models import Integration
    from app.security.crypto import decrypt
    from scripts.migrate_sqlite_to_pg import _migrate_integrations

    fernet = Fernet(get_settings().app_encryption_key.encode())

    # то, что владелец уже настроил руками: свой получатель, своя модель
    db.add(
        Integration(
            user_id=user.id,
            telegram_sender_id="128204572",
            openrouter_model="deepseek/deepseek-v4-flash-0731",
            openrouter_enabled=True,
        )
    )
    await db.commit()

    await _migrate_integrations(db, [_legacy_integration()], user.id, fernet)
    await db.commit()

    from sqlalchemy import select

    row = (
        await db.scalars(select(Integration).where(Integration.user_id == user.id))
    ).first()
    assert decrypt(row.openrouter_api_key_encrypted) == LEGACY_KEY, (
        "ключ OpenRouter не приехал: ON CONFLICT DO NOTHING пропустил строку, "
        "и перенос молча не сделал того единственного, ради чего затевался"
    )
    assert decrypt(row.telegram_bot_token_encrypted) == LEGACY_TOKEN
    assert decrypt(row.webhook_url_encrypted) == LEGACY_HOOK


async def test_migration_does_not_argue_with_the_owner(db, user):
    """Заполненное в назначении — осознанный выбор, источник его не трогает."""
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app.config import get_settings
    from app.models import Integration
    from scripts.migrate_sqlite_to_pg import _migrate_integrations

    fernet = Fernet(get_settings().app_encryption_key.encode())

    db.add(
        Integration(
            user_id=user.id,
            telegram_sender_id="128204572",
            openrouter_model="deepseek/deepseek-v4-flash-0731",
        )
    )
    await db.commit()

    await _migrate_integrations(db, [_legacy_integration()], user.id, fernet)
    await db.commit()

    row = (
        await db.scalars(select(Integration).where(Integration.user_id == user.id))
    ).first()
    assert row.telegram_sender_id == "128204572", (
        "перенос затёр получателя, которого владелец задал сам"
    )
    assert row.openrouter_model == "deepseek/deepseek-v4-flash-0731", (
        "перенос откатил модель к старой"
    )


async def test_channel_already_added_by_hand_is_not_duplicated(db, user):
    """Тот же канал с новым public_id — это тот же канал.

    Дедупликация по `(user_id, public_id)` его не узнаёт: заведённая руками
    строка получила свежий идентификатор. Узнавать надо по чату — иначе
    канал начнёт опрашиваться дважды и посты уедут в n8n парами.
    """
    from sqlalchemy import func, select

    from app.models import Monitor, MonitorChannel
    from scripts.migrate_sqlite_to_pg import _migrate_monitors

    # «Заведён руками» теперь значит «есть строка канала»: источник и канал
    # разъехались (Фаза 11), и узнавать чат надо по каналу.
    source = Monitor(
        user_id=user.id,
        public_id="новый-идентификатор",
        title="Finder.work",
        interval_minutes=360,  # владелец сам поставил 6 часов
    )
    db.add(source)
    await db.commit()
    db.add(
        MonitorChannel(
            monitor_id=source.id,
            user_id=user.id,
            chat_target="@finder",
            chat_title="Finder.work",
            chat_id=-100123,
            limit_count=20,
            extract_prompt="новый промпт",
        )
    )
    await db.commit()

    await _migrate_monitors(
        db,
        [
            _legacy_monitor("старый-uuid-1", -100123, "finder"),
            _legacy_monitor("старый-uuid-2", -100999, "другой"),
        ],
        user.id,
    )
    await db.commit()

    total = await db.scalar(
        select(func.count())
        .select_from(MonitorChannel)
        .where(MonitorChannel.user_id == user.id)
    )
    assert total == 2, (
        f"каналов стало {total}: существующий канал приехал вторым экземпляром "
        "и теперь опрашивается дважды"
    )

    kept = (
        await db.scalars(
            select(MonitorChannel).where(MonitorChannel.chat_id == -100123)
        )
    ).first()
    source = await db.get(Monitor, kept.monitor_id)
    assert source.interval_minutes == 360, (
        "перенос откатил интервал к старому значению — владелец менял его "
        "уже в новой сборке"
    )
    assert kept.extract_prompt == "новый промпт", "перенос затёр промпт канала"
