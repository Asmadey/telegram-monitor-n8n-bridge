"""Роутер админки (задача 2.8) — порт admin/base_controller.rb.

require_admin висит на РОУТЕРЕ: новый эндпоинт под /api/admin защищён
автоматически. Список юзеров — без хешей паролей, даже для админа.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_admin
from app.models import User

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


def _safe_user(user: User) -> dict[str, Any]:
    # password_hash наружу не отдаётся НИКОМУ, включая админа
    return {
        "id": user.id,
        "email": user.email,
        "timezone": user.timezone,
        "is_admin": user.is_admin,
        "created_at": user.created_at.isoformat(),
    }


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.scalars(select(User).order_by(User.id))
    return [_safe_user(u) for u in result]


@router.get("/users/{user_id}")
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    user = await db.get(User, user_id)
    if user is None:
        # админ уже аутентифицирован и знает, что юзеры существуют — 404 честен
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return _safe_user(user)
