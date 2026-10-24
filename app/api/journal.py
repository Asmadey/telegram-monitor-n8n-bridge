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
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import LogEntry, SentMessage
from app.services.journal import add_log

router = APIRouter(dependencies=[Depends(require_user)])
logger = logging.getLogger(__name__)

# «все статусы» — то же слово, что понимает текущий фронтенд
ANY_STATUS = "ALL"
# Тип записи, которой очистка журнала оставляет след; крестиком не удаляется
CLEAR_TRACE = "SYSTEM"
# Верхняя граница BIGINT: номер больше записи не называет
MAX_ID = 2**63 - 1


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
    offset: int = Query(0, ge=0),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    base = repo.query(SentMessage)
    total = await repo.db.scalar(
        base.with_only_columns(func.count(), maintain_column_froms=True)
    )
    rows = (
        await repo.db.scalars(
            base.order_by(SentMessage.id.desc()).offset(offset).limit(limit)
        )
    ).all()
    items = [_message(m) for m in rows]
    return {"total": total or 0, "messages": items}


@router.get("/api/logs")
async def list_logs(
    limit: int = Query(150, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str = Query(ANY_STATUS),
    # Курсор «Загрузить ещё» (13.29): записи старше этой. Сдвиг ломается от
    # удаления крестиком во время дозагрузки — страница съезжает, и одна
    # запись не показывается никогда; курсор от удалений не зависит.
    before_id: int | None = Query(None, ge=1),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    stmt = repo.query(LogEntry)
    if status and status != ANY_STATUS:
        stmt = stmt.where(LogEntry.status == status)
    total = await repo.db.scalar(
        stmt.with_only_columns(func.count(), maintain_column_froms=True)
    )
    page = stmt.order_by(LogEntry.id.desc())
    if before_id is not None:
        page = page.where(LogEntry.id < before_id)
    else:
        page = page.offset(offset)
    rows = (await repo.db.scalars(page.limit(limit))).all()

    sent_total = await repo.db.scalar(
        repo.query(SentMessage).with_only_columns(func.count())
    )
    entries = [_log(e) for e in rows]
    return {
        "total": total or 0,
        "total_sent_messages_db": sent_total or 0,
        "logs": entries,
    }


@router.delete("/api/logs")
async def clear_logs(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Очистка журнала — только своего (см. модуль-докстринг).

    Сама очистка записывается в журнал: действие, стирающее историю, обязано
    оставлять след, иначе его нельзя отследить постфактум.
    """
    await repo.db.execute(repo.delete(LogEntry))
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        CLEAR_TRACE,
        "Журнал событий очищен пользователем",
        "INFO",
    )
    return {"status": "cleared"}


@router.delete("/api/logs/{entry_id}")
async def delete_log_entry(
    entry_id: int, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    """Удалить одну запись журнала — крестик в конце строки (13.28).

    Чужая запись — 404, а не 403 (правило проекта). След, в отличие от
    очистки целиком, пишется не в журнал, а в журнал процесса: человек
    убирает строку, чтобы её не было, и строка «удалена запись» на её месте
    свела бы крестик на нет.

    Отметки «прочитано» (`sent_messages`) — другая таблица и не трогаются:
    удалённая запись журнала не делает пост снова новым.
    """
    # Номер вне BIGINT записи не называет — это 404, а не ошибка
    # переполнения базы, то есть 500 (13.29)
    entry = await repo.get(LogEntry, entry_id) if 0 < entry_id <= MAX_ID else None
    if entry is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if entry.event_type == CLEAR_TRACE:
        # След очистки — сам след действия, стирающего историю (см.
        # clear_logs). Крестик на нём стёр бы и действие, и его след (13.29).
        raise HTTPException(
            status_code=409,
            detail="Запись об очистке журнала не удаляется: это её след",
        )
    logger.info(
        "тенант %s удалил запись журнала %s (%s)",
        repo.user_id,
        entry_id,
        entry.event_type,
    )
    await repo.db.delete(entry)
    await repo.db.commit()
    return {"status": "deleted", "id": entry_id}
