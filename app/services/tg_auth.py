"""Провайдер Telegram-клиента для потока входа (задачи 3.3/4.1 PLAN.md).

Web-процесс поднимает КОРОТКОЖИВУЩИЙ клиент только на время авторизации
и сразу отключает — долгоживущие клиенты принадлежат воркеру (иначе два
процесса на одном auth-key → AUTH_KEY_DUPLICATED, Telegram может убить
сессию). Сессия — StringSession (файла больше нет, задача 3.2).

Ключи приложения принадлежат ПОЛЬЗОВАТЕЛЮ (открытый вопрос №1 решён
2026-09-07): они берутся из его кабинета, а не из ENV. Ключа сервиса нет —
подставлять вместо чужого нечего, и отказ поэтому громкий. Тесты подменяют
эту зависимость фейком, живого Telegram в юнит-прогонах нет.
"""

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services.tg_credentials import CredentialsMissing, require_credentials


async def get_telegram_auth_client(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[TelegramClient]:
    try:
        api_id, api_hash = await require_credentials(db, user.id)
    except CredentialsMissing as exc:
        # 400, а не 503: не сервис сломан — пользователю нечем подключаться
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
