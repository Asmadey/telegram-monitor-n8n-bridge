"""Ключи приложения MTProto, принадлежащие пользователю (открытый вопрос №1).

Владелец решил 2026-09-07: каждый пользователь регистрирует своё приложение
на my.telegram.org и вносит `api_id`/`api_hash` в свой кабинет; подключение
его Telegram-аккаунта идёт только через них. Общий ключ сервиса не
подставляется никогда — молчаливый откат на него выглядел бы как «работает»
ровно до дня, когда Telegram ограничит приложение, через которое логинятся
все сразу.

`api_hash` — секрет того же класса, что ключ OpenRouter: в базе только
зашифрованным (3.4), наружу — только признак наличия (0.3).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import TenantRepo
from app.models import TelegramCredential
from app.security.crypto import decrypt, encrypt


class CredentialsMissing(RuntimeError):
    """У пользователя нет своих ключей — вход в Telegram начинать нечем."""


async def get_row(db: AsyncSession, user_id: int) -> TelegramCredential | None:
    return (await db.scalars(TenantRepo(db, user_id).query(TelegramCredential))).first()


async def save_credentials(
    db: AsyncSession, user_id: int, *, api_id: int, api_hash: str | None = None
) -> TelegramCredential:
    """Сохранить ключи пользователя.

    `api_hash=None` означает «поле не передано» и НЕ затирает сохранённое
    (правило 0.3): иначе сохранение формы, где пользователь поправил только
    номер приложения, вытирает хеш — и подключение ломается молча.
    """
    row = await get_row(db, user_id)
    if row is None:
        if not api_hash:
            raise CredentialsMissing(
                "api_hash обязателен при первом сохранении ключей Telegram"
            )
        row = TelegramCredential(
            user_id=user_id, api_id=api_id, api_hash_encrypted=encrypt(api_hash)
        )
        db.add(row)
        await db.flush()
        return row

    row.api_id = api_id
    if api_hash:
        row.api_hash_encrypted = encrypt(api_hash)
    await db.flush()
    return row


async def load_credentials(db: AsyncSession, user_id: int) -> tuple[int, str] | None:
    """Пара (api_id, api_hash) владельца или None, если он их не вносил."""
    row = await get_row(db, user_id)
    if row is None:
        return None
    return row.api_id, decrypt(row.api_hash_encrypted)


async def require_credentials(db: AsyncSession, user_id: int) -> tuple[int, str]:
    """То же, но отказ громкий: подставлять чужой ключ вместо своего нельзя."""
    pair = await load_credentials(db, user_id)
    if pair is None:
        raise CredentialsMissing(
            "Не заданы api_id и api_hash вашего приложения Telegram. "
            "Получите их на my.telegram.org и сохраните в кабинете."
        )
    return pair


async def credentials_for_owner(db: AsyncSession, user_id: int) -> tuple[int, str]:
    """Ключи владельца для фонового процесса (воркер)."""
    return await require_credentials(db, user_id)


__all__ = [
    "CredentialsMissing",
    "credentials_for_owner",
    "get_row",
    "load_credentials",
    "require_credentials",
    "save_credentials",
]
