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

from app.db import TenantRepo, get_db
from app.deps import require_user
from app.models import TelegramAccount, User
from app.security.crypto import decrypt
from app.services.tg_attempts import attempt_session
from app.services.tg_credentials import CredentialsMissing, require_credentials


async def get_telegram_auth_client(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[TelegramClient]:
    """Клиент ТЕКУЩЕЙ попытки входа, а не просто новый клиент.

    Telegram привязывает попытку к auth-key той сессии, которая вызвала
    `send_code_request`. Пока каждый запрос поднимал пустую `StringSession`,
    `sign_in` шёл с другим auth-key — и Telegram отвечал
    `PHONE_CODE_EXPIRED`: для новой сессии код не существовал никогда.
    Найдено на живом Telegram 2026-09-07; двойник в тестах возвращал один
    объект на оба шага и потому этого не показывал.

    Поэтому: есть незавершённая попытка — продолжаем ЕЁ сессию; нет —
    начинаем с чистой.
    """
    try:
        api_id, api_hash = await require_credentials(db, user.id)
    except CredentialsMissing as exc:
        # 400, а не 503: не сервис сломан — пользователю нечем подключаться
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client = TelegramClient(
        StringSession(await attempt_session(db, user.id)),
        api_id,
        api_hash,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def get_account_client(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[TelegramClient]:
    """Короткоживущий клиент ПОДКЛЮЧЁННОГО аккаунта.

    Второй клиент рядом с клиентом входа — и различать их обязательно.
    Клиент входа живёт пустой сессией (или сессией незавершённой попытки);
    любое действие от имени аккаунта — разрешение канала, список диалогов —
    требует СОХРАНЁННОЙ сессии пользователя. Пока разрешение канала брало
    клиента у зависимости входа, Telegram отвечал AUTH_KEY_UNREGISTERED:
    у пустой сессии нет зарегистрированного пользователя (найдено на живом
    аккаунте 2026-09-07).

    Клиент короткоживущий — открыт ровно на запрос: долгоживущие
    принадлежат воркеру, иначе два процесса на одном auth-key.
    """
    account = (await db.scalars(TenantRepo(db, user.id).query(TelegramAccount))).first()
    if account is None or account.status != "active":
        raise HTTPException(status_code=400, detail="Telegram-аккаунт не подключён")
    try:
        api_id, api_hash = await require_credentials(db, user.id)
    except CredentialsMissing as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client = TelegramClient(
        StringSession(decrypt(account.session_string_encrypted)),
        api_id,
        api_hash,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
