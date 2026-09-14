"""Экспорт и удаление аккаунта (задача 13.2).

Закрыт входом целиком: и выгрузка, и удаление — действия над собственными
данными, и анониму здесь делать нечего.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_user
from app.models import User
from app.services.account import delete_account, export_account

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user)])


class DeleteRequest(BaseModel):
    confirm: str = ""


@router.get("/api/account/export")
async def export_my_account(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await export_account(db, user)


@router.delete("/api/account")
async def delete_my_account(
    req: DeleteRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Удалить аккаунт целиком. Необратимо.

    Подтверждение — собственный адрес, а не «да»: необратимое действие не
    должно случаться с одного нажатия, а адрес нельзя набрать по инерции.
    Сравнение без учёта регистра и пробелов: спотыкаться на Caps Lock в
    момент ухода — издевательство, а не защита.
    """
    if req.confirm.strip().lower() != (user.email or "").strip().lower():
        raise HTTPException(
            status_code=400,
            detail="Подтвердите удаление: введите адрес своей учётной записи",
        )

    from app.services.tg_session import revoke_telegram_session

    await delete_account(db, user, revoker=revoke_telegram_session)
    logger.info("аккаунт удалён по запросу владельца")
    return {"status": "deleted"}
