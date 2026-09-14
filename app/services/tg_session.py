"""Завершение MTProto-сессии в самом Telegram (задача 13.2).

Клиент здесь КОРОТКОЖИВУЩИЙ — тот же порядок, что у потока входа (3.3):
долгоживущие принадлежат воркеру, иначе два процесса на одном auth-key дают
`AUTH_KEY_DUPLICATED` и Telegram может убить сессию сам.

Зачем вообще: стереть строку — значит убрать доступ у СЕРВИСА, но сессия
остаётся в списке устройств пользователя, а уцелевшая копия строки
(резервная копия базы) продолжает работать. `log_out` закрывает и это.
"""

import logging

from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.models import TelegramAccount, TelegramCredential
from app.security.crypto import decrypt

logger = logging.getLogger(__name__)


async def revoke_telegram_session(db, user_id: int, *, client_factory=None) -> bool:
    """Завершить сессию тенанта в Telegram. False — завершать было нечего."""
    account = (
        await db.scalars(
            select(TelegramAccount).where(TelegramAccount.user_id == user_id)
        )
    ).first()
    if account is None or not account.session_string_encrypted:
        return False

    credentials = (
        await db.scalars(
            select(TelegramCredential).where(TelegramCredential.user_id == user_id)
        )
    ).first()
    if credentials is None:
        # Ключей приложения нет — подключиться нечем. Общего ключа сервиса у
        # нас нет принципиально (открытый вопрос №1), подставлять нечего.
        return False

    session = StringSession(decrypt(account.session_string_encrypted))
    api_hash = decrypt(credentials.api_hash_encrypted)
    factory = client_factory or (
        lambda: TelegramClient(session, credentials.api_id, api_hash)
    )
    client = factory()
    try:
        await client.connect()
        await client.log_out()
        return True
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — отключение не стоит падения удаления
            logger.debug("клиент не отключился штатно", exc_info=True)
