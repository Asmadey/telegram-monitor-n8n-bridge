"""Настройки и запуск автоочистки (К2) — порт server.py:1832-1860.

Срок хранения — настройка тенанта: в монолите она лежала в общей таблице
settings, одна на весь сервис. Данные принадлежат пользователю, и решение,
сколько их хранить, тоже.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import Integration
from app.services.cleanup import _utcnow, purge_older_than
from app.services.journal import add_log

router = APIRouter(dependencies=[Depends(require_user)])

# те же варианты, что предлагает интерфейс монолита
ALLOWED_DAYS = (7, 14, 30, 60, 90)


class CleanupConfig(BaseModel):
    enabled: bool
    days: int = Field(default=30)


async def _row(repo: TenantRepo) -> Integration | None:
    return (
        await repo.db.scalars(
            select(Integration).where(Integration.user_id == repo.user_id)
        )
    ).first()


async def _row_or_create(repo: TenantRepo) -> Integration:
    row = await _row(repo)
    if row is None:
        row = Integration(user_id=repo.user_id)
        repo.db.add(row)
        await repo.db.commit()
        await repo.db.refresh(row)
    return row


@router.get("/api/cleanup")
async def get_cleanup(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    row = await _row(repo)
    return {
        "enabled": bool(row.cleanup_enabled) if row else False,
        "days": row.cleanup_days if row else 30,
        "last_run": row.cleanup_last_run if row else None,
    }


@router.post("/api/cleanup")
async def save_cleanup(
    req: CleanupConfig, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    if req.days not in ALLOWED_DAYS:
        # список закрыт намеренно: срок в один день молча стёр бы дедупликацию
        raise HTTPException(
            status_code=400,
            detail=f"Допустимые сроки хранения: {', '.join(map(str, ALLOWED_DAYS))}",
        )
    row = await _row_or_create(repo)
    row.cleanup_enabled = req.enabled
    row.cleanup_days = req.days
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        "SETTINGS",
        f"Автоочистка: {'вкл' if req.enabled else 'выкл'}, срок {req.days} дн.",
        "SUCCESS",
    )
    return await get_cleanup(repo)


@router.post("/api/cleanup/run-now")
async def run_cleanup_now(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    row = await _row_or_create(repo)
    removed = await purge_older_than(repo.db, repo.user_id, row.cleanup_days)
    row.cleanup_last_run = _utcnow()
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        "AUTO_CLEANUP",
        f"Очистка (> {row.cleanup_days} дн.): {removed}",
        "SUCCESS",
    )
    return {"status": "done", "removed": removed}
