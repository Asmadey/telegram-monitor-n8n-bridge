"""Шлюз к Telegram для воркера: клиент тенанта, цель, посты, аватарка.

Зачем отдельный объект, а не прямые вызовы из цикла: воркер обязан быть
проверяемым без живого MTProto. Шлюз — единственная граница с Telegram,
и в тестах он заменяется двойником целиком; всё остальное (очередь,
расписание, дедупликация, доставка) гоняется по-настоящему.

Долгоживущие клиенты берутся из пула (3.5) и принадлежат ТОЛЬКО воркеру:
второй процесс на том же auth-key даёт AUTH_KEY_DUPLICATED, а это выбивает
пользователя из его собственного аккаунта.
"""

import logging

from sqlalchemy import select

from app.models import TelegramAccount
from app.security.crypto import decrypt
from app.services.messages import fetch_channel_messages

logger = logging.getLogger(__name__)


class TelegramGateway:
    def __init__(self, pool):
        self.pool = pool

    async def client_for(self, db, user_id: int):
        """Клиент пользователя или None, если аккаунт не подключён.

        None — штатный случай (зарегистрировался, каналы не добавил), а не
        ошибка: цикл просто пропускает такого пользователя.
        """
        account = (
            await db.scalars(
                select(TelegramAccount).where(TelegramAccount.user_id == user_id)
            )
        ).first()
        if account is None or account.status != "active":
            return None
        return await self.pool.get(user_id, decrypt(account.session_string_encrypted))

    async def resolve(self, client, target: str):
        return await client.get_entity(target)

    async def fetch(self, client, entity, *, limit: int, offset_hours: int | None):
        return await fetch_channel_messages(
            client, entity, limit=limit, offset_hours=offset_hours
        )

    async def avatar(self, client, entity) -> bytes | None:
        """Аватарка канала. Отсутствие фото — не ошибка, поэтому None."""
        try:
            return await client.download_profile_photo(
                entity, file=bytes, download_big=False
            )
        except Exception:  # noqa: BLE001 — картинка не стоит падения опроса
            logger.debug("аватарка канала недоступна", exc_info=True)
            return None
