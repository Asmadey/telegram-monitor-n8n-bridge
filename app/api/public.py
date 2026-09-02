"""Публичные маршруты — единственные, куда анониму можно (задача 2.3).

Это opt-out из «закрыто по умолчанию»: каждый новый публичный путь обязан
попасть в белый список tests/test_22_auth_required.py — иначе тест его
заблокирует, и добавляющий сразу увидит, что открыл лишнее.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    # только статус: аккаунтные данные (id, username) из /health убраны
    return {"status": "ok"}
