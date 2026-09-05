"""Сохранённые посты и журнал событий (К2) — порт server.py:1252, 1794-1829.

Два отличия от монолита, помимо закрытия эндпоинтов авторизацией.

1. `DELETE /api/logs` в server.py выполняет `DELETE FROM logs` без единого
   условия. В мульти-тенанте это стирает журнал ВСЕМ пользователям сервиса:
   один клиент нажимает «очистить» — остальные теряют историю. Здесь
   удаление ограничено строками текущего пользователя.

2. Реакции постов отдаются разобранным массивом, а не строкой JSON, как их
   хранит база. Иначе каждый потребитель разбирает их сам и по-своему —
   а битая строка роняет отрисовку всей вкладки, вместо одной карточки.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import LogEntry, SentMessage
from app.services.journal import add_log

router = APIRouter(dependencies=[Depends(require_user)])

# «все статусы» — то же слово, что понимает текущий фронтенд
ANY_STATUS = "ALL"


def _reactions(raw: str | None) -> list[dict]:
    """Битая строка — пустой список, а не 500 на всю вкладку."""
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _message(m: SentMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "date": m.date,
        "sender": m.sender,
        "text": m.text,
        "views": m.views,
        "forwards": m.forwards,
        "has_media": m.has_media,
        "reactions_count": m.reactions_count,
        "reactions": _reactions(m.reactions_json),
        "post_url": m.post_url,
        "sent_at": m.sent_at,
    }


def _log(entry: LogEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "timestamp": entry.timestamp,
        "event_type": entry.event_type,
        "chat_title": entry.chat_title,
        "chat_id": entry.chat_id,
        "messages_count": entry.messages_count,
        "status": entry.status,
        "details": entry.details,
    }


@router.get("/api/messages")
async def list_messages(
    limit: int = Query(100, ge=1, le=500),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    rows = (
        await repo.db.scalars(
            repo.query(SentMessage).order_by(SentMessage.id.desc()).limit(limit)
        )
    ).all()
    items = [_message(m) for m in rows]
    return {"total": len(items), "messages": items}


@router.get("/api/logs")
async def list_logs(
    limit: int = Query(150, ge=1, le=1000),
    status: str = Query(ANY_STATUS),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    stmt = repo.query(LogEntry)
    if status and status != ANY_STATUS:
        stmt = stmt.where(LogEntry.status == status)
    rows = (await repo.db.scalars(stmt.order_by(LogEntry.id.desc()).limit(limit))).all()

    sent_total = await repo.db.scalar(
        repo.query(SentMessage).with_only_columns(func.count())
    )
    entries = [_log(e) for e in rows]
    return {
        "total": len(entries),
        "total_sent_messages_db": sent_total or 0,
        "logs": entries,
    }


@router.delete("/api/logs")
async def clear_logs(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Очистка журнала — только своего (см. модуль-докстринг).

    Сама очистка записывается в журнал: действие, стирающее историю, обязано
    оставлять след, иначе его нельзя отследить постфактум.
    """
    await repo.db.execute(delete(LogEntry).where(LogEntry.user_id == repo.user_id))
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        "SYSTEM",
        "Журнал событий очищен пользователем",
        "INFO",
    )
    return {"status": "cleared"}
