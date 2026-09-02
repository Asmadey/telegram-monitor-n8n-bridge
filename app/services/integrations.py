"""Секреты интеграций тенанта (задача 3.4 PLAN.md) — только зашифрованными.

Колонки *_encrypted в integrations заполняются ИСКЛЮЧИТЕЛЬНО через этот
модуль: писать туда открытым текстом нельзя нигде (скрипт миграции 1.5
шифрует при переносе, рантайм — здесь). Читаются расшифрованными тоже
здесь — роутеры Фазы 5 получают dict, а не сырую строку.

Один пользователь — одна строка integrations (unique user_id):
сохранение всегда upsert, опущенные секреты не затирают записанные
(пустой POST не должен вытирать ключ, см. задачу 0.3).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Integration
from app.security.crypto import decrypt, encrypt


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
    if bot_token:
        row.telegram_bot_token_encrypted = encrypt(bot_token)
    if openrouter_api_key:
        row.openrouter_api_key_encrypted = encrypt(openrouter_api_key)
    if webhook_url:
        row.webhook_url_encrypted = encrypt(webhook_url)
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
