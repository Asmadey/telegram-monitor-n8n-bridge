"""Диагностика состояния сервиса (задача 10.2).

Закрыт входом: подробности состояния — это подробности устройства.
Публичным остаётся только `/health` с голым `{"status": "ok"}` (4.7, C22):
анониму знать, что у сервиса отстаёт очередь, незачем.
"""

from fastapi import APIRouter, Depends

from app.db import get_db
from app.deps import require_user
from app.services.ops import ops_status

router = APIRouter(dependencies=[Depends(require_user)])


@router.get("/api/ops/health")
async def ops_health(db=Depends(get_db)) -> dict:
    return await ops_status(db)
