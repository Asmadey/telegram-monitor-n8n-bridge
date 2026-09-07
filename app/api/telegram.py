"""Поток входа в Telegram (задача 3.3 PLAN.md) — порт /api/auth/* из server.py.

Отличия от server.py, ради которых задача и существует:
- глобального словаря auth_state НЕТ: состояние попытки — строка
  tg_auth_attempts текущего юзера (TTL 10 минут), два параллельных входа
  не могут перетереть друг друга;
- sign-in берёт phone/phone_code_hash СТРОГО из строки юзера — из тела
  их принять нельзя (тело не содержит этих полей вообще);
- send-code: лимит 3/час (задача 2.7 — защита аккаунта КЛИЕНТА от
  FloodWait), новая попытка отменяет старую, hash наружу не отдаётся;
- успешный вход сохраняет MTProto-сессию зашифрованной (задача 3.2) и
  съедает попытку.
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import TenantRepo, get_db
from app.deps import get_tenant_repo, require_user
from app.models import TelegramAccount, TgAuthAttempt, User
from app.security.crypto import encrypt
from app.security.ratelimit import TELEGRAM_SEND_CODE_LIMIT, limiter
from app.security.sessions import _utc
from app.services.tg_account import save_tg_session
from app.services.tg_auth import get_account_client, get_telegram_auth_client
from app.services.tg_credentials import (
    CredentialsMissing,
    save_credentials,
)
from app.services.tg_credentials import get_row as get_credentials_row

router = APIRouter(dependencies=[Depends(require_user)])

# TTL попытки входа (план 3.3): 10 минут
ATTEMPT_TTL = datetime.timedelta(minutes=10)


class PhoneRequest(BaseModel):
    phone: str


class CredentialsRequest(BaseModel):
    """Ключи приложения пользователя с my.telegram.org.

    `api_hash=None` — «поле не передано»: сохранённый хеш не затирается
    (правило 0.3). Пустая строка тоже не затирает: ключи без хеша
    бессмысленны, очистка делается отключением аккаунта.
    """

    api_id: int
    api_hash: str | None = None


class SignInRequest(BaseModel):
    # НИКАКИХ phone/phone_code_hash из тела: только строка юзера в БД.
    # Сюда же просачивается попытка закончить чужой вход.
    code: str
    password: str | None = None


@router.get("/api/telegram/status")
async def telegram_status(
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    """Ключи и аккаунт ТЕКУЩЕГО пользователя; api_hash наружу не уходит."""
    account = (await repo.db.scalars(repo.query(TelegramAccount).limit(1))).first()
    credentials = await get_credentials_row(repo.db, repo.user_id)
    return {
        "api_id": credentials.api_id if credentials is not None else None,
        "has_api_hash": credentials is not None,
        "is_authorized": account is not None,
        "user": (
            {
                "id": account.tg_user_id,
                "username": account.tg_username,
                "phone": account.phone,
            }
            if account is not None
            else None
        ),
    }


@router.post("/api/telegram/credentials")
async def save_telegram_credentials(
    req: CredentialsRequest,
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    """Сохранить СВОИ ключи приложения Telegram.

    Наружу возвращается только признак наличия хеша: сам он не уходит
    отсюда никогда — ни в ответе, ни в статусе (0.3).
    """
    try:
        row = await save_credentials(
            repo.db, repo.user_id, api_id=req.api_id, api_hash=req.api_hash
        )
    except CredentialsMissing as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await repo.db.commit()
    return {"api_id": row.api_id, "has_api_hash": True}


@router.post("/api/telegram/send-code")
@limiter.limit(TELEGRAM_SEND_CODE_LIMIT)
async def send_code(
    request: Request,
    req: PhoneRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    client=Depends(get_telegram_auth_client),
) -> dict:
    """Запросить код подтверждения. 3/час (защита аккаунта клиента:
    Telegram отвечает на спам кодов FloodWait и банит на дни)."""
    phone = req.phone.strip()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:  # noqa: BLE001 — текст ошибки показывается юзеру
        # FloodWaitError НЕ ретраим сразу (правило AGENTS.md): отказ честный
        raise HTTPException(status_code=400, detail=f"Ошибка отправки кода: {e}")

    # новая попытка отменяет старую: одна активная на юзера
    await db.execute(delete(TgAuthAttempt).where(TgAuthAttempt.user_id == user.id))
    expires_at = datetime.datetime.now(datetime.timezone.utc) + ATTEMPT_TTL
    db.add(
        TgAuthAttempt(
            user_id=user.id,
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            # Сессия, ЗАПРОСИВШАЯ код: подтверждать его обязана она же,
            # иначе Telegram отвечает PHONE_CODE_EXPIRED (2026-09-07).
            session_string_encrypted=encrypt(client.session.save()),
            expires_at=expires_at,
        )
    )
    await db.commit()
    # phone_code_hash наружу не отдаётся: с ним вход можно завершить минуя БД
    return {
        "status": "code_sent",
        "phone": phone,
        "message": f"Код подтверждения отправлен в приложение Telegram для {phone}",
    }


@router.post("/api/telegram/sign-in")
async def sign_in(
    req: SignInRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    client=Depends(get_telegram_auth_client),
) -> dict:
    """Завершить вход: phone/phone_code_hash — ТОЛЬКО из строки текущего
    юзера. Чужой hash из тела здесь физически не применим."""
    attempt = (
        await db.scalars(
            select(TgAuthAttempt)
            .where(TgAuthAttempt.user_id == user.id)
            .order_by(TgAuthAttempt.id.desc())
        )
    ).first()
    now = _utc(datetime.datetime.now(datetime.timezone.utc))
    if attempt is None or _utc(attempt.expires_at) < now:
        raise HTTPException(
            status_code=400, detail="Сначала запросите код подтверждения"
        )

    from telethon import errors

    try:
        await client.sign_in(
            phone=attempt.phone,
            code=req.code.strip(),
            phone_code_hash=attempt.phone_code_hash,
        )
    except errors.SessionPasswordNeededError:
        if not req.password:
            return {
                "status": "2fa_required",
                "message": "Включена двухфакторная аутентификация. Введите пароль 2FA.",
            }
        try:
            await client.sign_in(password=req.password)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Неверный пароль 2FA: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Ошибка авторизации: {e}")

    if not await client.is_user_authorized():
        raise HTTPException(status_code=400, detail="Не удалось завершить авторизацию")

    me = await client.get_me()
    # сессия — в БД зашифрованной (задача 3.2), файл больше не нужен
    await save_tg_session(
        db,
        user.id,
        client.session.save(),
        phone=attempt.phone,
        tg_user_id=me.id,
        tg_username=getattr(me, "username", None),
    )
    # попытка съедена: повторный sign-in по той же строке невозможен
    await db.execute(delete(TgAuthAttempt).where(TgAuthAttempt.user_id == user.id))
    await db.commit()
    return {
        "status": "authorized",
        "message": "Авторизация успешно завершена",
        "user": {
            "id": me.id,
            "first_name": getattr(me, "first_name", ""),
            "username": getattr(me, "username", None),
        },
    }


async def get_dialog_lister(client=Depends(get_account_client)):
    """Список диалогов аккаунта пользователя.

    Клиент — общий для всех действий аккаунта (`get_account_client`):
    сессия подключённого пользователя, открыт на время запроса. Своя копия
    этой сборки жила здесь до 2026-09-07 и разошлась бы с резолвером
    каналов при первой же правке — а разойтись тут значит выполнить
    действие не той сессией.

    В монолите (server.py:1233) этот эндпоинт был открыт всему интернету и
    отдавал список ВСЕХ чатов и переписок владельца аккаунта.
    """

    async def lister(limit: int = 50):
        return [d async for d in client.iter_dialogs(limit=limit)]

    return lister


@router.get("/api/telegram/dialogs")
async def dialogs(limit: int = 50, lister=Depends(get_dialog_lister)) -> dict:
    items = []
    for d in await lister(limit):
        entity = getattr(d, "entity", None)
        items.append(
            {
                "id": getattr(d, "id", None),
                "name": getattr(d, "name", None),
                "username": getattr(entity, "username", None),
                "type": (
                    "channel"
                    if getattr(d, "is_channel", False)
                    else "group"
                    if getattr(d, "is_group", False)
                    else "user"
                ),
            }
        )
    return {"total": len(items), "dialogs": items}


@router.post("/api/telegram/logout")
async def logout(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Отключить Telegram-аккаунт: сохранённая MTProto-сессия стирается.

    Оставить её после «выйти» — значит оставить доступ к аккаунту, который
    пользователь считает отключённым. Удаление ограничено своим user_id:
    выход одного не должен отключать других.
    """
    await db.execute(delete(TelegramAccount).where(TelegramAccount.user_id == user.id))
    await db.execute(delete(TgAuthAttempt).where(TgAuthAttempt.user_id == user.id))
    await db.commit()
    return {"status": "logged_out"}
