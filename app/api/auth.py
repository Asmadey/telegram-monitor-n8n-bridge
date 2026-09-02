"""Роутер аутентификации. /auth/me — первый защищённый эндпоинт (задача 2.3).

Закрыто по умолчанию: Depends(require_user) висит на РОУТЕРЕ, а не на каждом
эндпоинте руками — новый эндпоинт в этом роутере защищён автоматически.
/signup и /login (задача 2.4) появятся в отдельном публичном роутере.
"""
from fastapi import APIRouter, Depends

from app.deps import require_user
from app.models import User

router = APIRouter(dependencies=[Depends(require_user)])


@router.get("/auth/me")
async def me(user: User = Depends(require_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "timezone": user.timezone,
        "is_admin": user.is_admin,
    }