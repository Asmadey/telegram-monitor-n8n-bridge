"""Секреты интеграций тенанта (задача 3.4 PLAN.md) — только зашифрованными.

Колонки *_encrypted в integrations заполняются ИСКЛЮЧИТЕЛЬНО через этот
модуль: писать туда открытым текстом нельзя нигде (скрипт миграции 1.5
шифрует при переносе, рантайм — здесь). Читаются расшифрованными тоже
здесь — роутеры Фазы 5 получают dict, а не сырую строку.

Один пользователь — одна строка integrations (unique user_id):
сохранение всегда upsert. Контракт секретов (С16, 4.7): None — «поле
не передано», НЕ затирает записанное (пустой POST не вытирает ключ,
см. задачу 0.3); "" — «очистить», колонка обнуляется. Конфигурация
(update_integration_config) идёт по белому списку несекретных колонок.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Integration
from app.security.crypto import decrypt, encrypt

# несекретные колонки, обновляемые конфигурационным путём (4.7); секреты
# (telegram_bot_token, openrouter_api_key, webhook_url) сюда НЕ входят
_CONFIG_FIELDS = frozenset(
    {
        "telegram_sender_id",
        "telegram_forward_enabled",
        "openrouter_base_url",
        "openrouter_model",
        "openrouter_enabled",
        "auto_webhook_enabled",
    }
)


async def save_integration_secrets(
    db: AsyncSession,
    user_id: int,
    *,
    bot_token: str | None = None,
    openrouter_api_key: str | None = None,
    webhook_url: str | None = None,
) -> Integration:
    """Сохранить секреты интеграций зашифрованными (upsert по user_id)."""
    row = (
        await db.scalars(select(Integration).where(Integration.user_id == user_id))
    ).first()
    if row is None:
        row = Integration(user_id=user_id)
        db.add(row)
    # С16 (4.7): None — «поле не передано» (не трогаем, 0.3), "" —
    # «очистить» (encrypt("") не звать: пустая колонка = пустой секрет).
    if bot_token is not None:
        row.telegram_bot_token_encrypted = encrypt(bot_token) if bot_token else ""
    if openrouter_api_key is not None:
        row.openrouter_api_key_encrypted = (
            encrypt(openrouter_api_key) if openrouter_api_key else ""
        )
    if webhook_url is not None:
        row.webhook_url_encrypted = encrypt(webhook_url) if webhook_url else ""
    await db.commit()
    return row


def integration_secrets(row: Integration) -> dict[str, str]:
    """Расшифрованные секреты строки интеграций — для воркера/роутеров."""
    return {
        "telegram_bot_token": (
            decrypt(row.telegram_bot_token_encrypted)
            if row.telegram_bot_token_encrypted
            else ""
        ),
        "openrouter_api_key": (
            decrypt(row.openrouter_api_key_encrypted)
            if row.openrouter_api_key_encrypted
            else ""
        ),
        "webhook_url": (
            decrypt(row.webhook_url_encrypted) if row.webhook_url_encrypted else ""
        ),
    }


async def update_integration_config(
    db: AsyncSession, user_id: int, data: dict
) -> Integration:
    """Обновить НЕСЕКРЕТНЫЕ настройки интеграций (порт server.py:251).

    Оригинал строил `SET {k} = ?` из ключей словаря — имя колонки из
    пользовательских данных. Белый список (4.7): ключ вне списка —
    ValueError, а не молчаливая запись чужой колонки; секретные поля
    сюда НЕ входят — только через save_integration_secrets (3.4).
    """
    unknown = set(data) - _CONFIG_FIELDS
    if unknown:
        raise ValueError(f"неизвестные поля конфигурации: {sorted(unknown)}")
    row = (
        await db.scalars(select(Integration).where(Integration.user_id == user_id))
    ).first()
    if row is None:
        row = Integration(user_id=user_id)
        db.add(row)
    for key, value in data.items():
        setattr(row, key, value)
    await db.commit()
    return row
