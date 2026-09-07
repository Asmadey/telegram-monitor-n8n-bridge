"""Хранение Telegram-аккаунта тенанта (задача 3.2 PLAN.md).

Замена файла personal_account.session: строка StringSession живёт в
telegram_accounts, ЗАШИФРОВАННОЙ (app.security.crypto). У пользователя
один Telegram-аккаунт (unique user_id) — сохранение всегда upsert.

phone/tg_user_id/tg_username заполняются потоком входа (задача 3.3):
save_tg_session вызывается после успешного sign-in. При повторном
сохранении опущенные поля не затирают уже записанные.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TelegramAccount
from app.security.crypto import encrypt


async def save_tg_session(
    db: AsyncSession,
    user_id: int,
    session_string: str,
    *,
    phone: str = "",
    tg_user_id: int = 0,
    tg_username: str | None = None,
) -> TelegramAccount:
    encrypted = encrypt(session_string)
    account = (
        await db.scalars(
            select(TelegramAccount).where(TelegramAccount.user_id == user_id)
        )
    ).first()
    if account is None:
        account = TelegramAccount(
            user_id=user_id,
            phone=phone,
            session_string_encrypted=encrypted,
            tg_user_id=tg_user_id,
        )
        db.add(account)
    else:
        account.session_string_encrypted = encrypted
        if phone:
            account.phone = phone
        if tg_user_id:
            account.tg_user_id = tg_user_id
    if tg_username is not None:
        account.tg_username = tg_username
    await db.commit()
    return account
