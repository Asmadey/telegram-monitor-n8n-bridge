"""Провайдер Telegram-клиента для потока входа (задачи 3.3/4.1 PLAN.md).

Web-процесс поднимает КОРОТКОЖИВУЩИЙ клиент только на время авторизации
и сразу отключает — долгоживущие клиенты принадлежат воркеру (иначе два
процесса на одном auth-key → AUTH_KEY_DUPLICATED, Telegram может убить
сессию). Сессия — StringSession (файла больше нет, задача 3.2).

Ключи приложения — свои, из ENV (telegram_api_id/hash); тесты подменяют
эту зависимость фейком, живого Telegram в юнит-прогонах нет.
"""

from collections.abc import AsyncIterator

from fastapi import HTTPException
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import get_settings


async def get_telegram_auth_client() -> AsyncIterator[TelegramClient]:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        # громко: «вход в Telegram недоступен» лучше молчаливой заглушки
        raise HTTPException(
            status_code=503,
            detail="Telegram API не настроен (TELEGRAM_API_ID/TELEGRAM_API_HASH)",
        )
    client = TelegramClient(
        StringSession(),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
