"""Лента и аватарки (задача 5.4 PLAN.md) — первый ресурсный роутер из server.py.

Старая лента (server.py:1842) возила в КАЖДОЙ строке GET /api/feed и
photo_base64, и raw_messages_json целиком: двести аватарок — мегабайты на
каждый опрос (лента обновляется каждые 30 секунд). Здесь:
- список — только метаданные карточек: ни фото, ни сырых постов
  (красный тест: ответ < 200 КБ и без data:image);
- исходные посты — детальным видом GET /api/feed/{id};
- аватарка — отдельным GET /api/avatars/{chat_id} с Cache-Control:
  браузер кеширует на сутки, лента не раздувается.

Изоляция: роутер целиком за require_user (закрыто по умолчанию, 2.3),
тенантные чтения — только через TenantRepo: чужой id → None → 404
(«найдено, но чужое» = 403 подтверждает существование). Аватарка — фото
публичного канала без user_id, но эндпоинт сначала проверяет, что
ТЕКУЩИЙ юзер мониторит канал: произвольным клиентам существование
канала не подтверждаем.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete

from app.db import TenantRepo, deleted_count
from app.deps import get_tenant_repo, require_user
from app.models import ChatAvatar, FeedItem, Monitor
from app.services.jobs import enqueue_job

router = APIRouter(dependencies=[Depends(require_user)])

# private: ответ зависит от того, кто просит (свой монитор или нет), —
# общий прокси-кеш это кешировать не должен
AVATAR_CACHE_CONTROL = "private, max-age=86400"

# поля списочной карточки: БЕЗ photo_base64 (в строке её больше нет) и
# БЕЗ raw_messages_json/messages — посты отдаёт детальный вид
_LIST_FIELDS = (
    "id",
    "job_id",
    "created_at",
    "chat_id",
    "chat_title",
    "chat_username",
    "messages_count",
    "ai_analysis",
    "model_name",
    "delivery_status",
)


def _card(item: FeedItem) -> dict:
    return {name: getattr(item, name) for name in _LIST_FIELDS}


@router.get("/api/feed")
async def list_feed(
    limit: int = Query(50, ge=1, le=200),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    """Список карточек ленты — только метаданные (тяжёлое — детальным видом)."""
    stmt = repo.query(FeedItem).order_by(FeedItem.id.desc()).limit(limit)
    items = (await repo.db.scalars(stmt)).all()
    cards = [_card(i) for i in items]
    return {"total": len(cards), "feed": cards}


@router.get("/api/feed/{id}")
async def feed_detail(id: int, repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Детальный вид: то же + распарсенные messages (raw_messages_json
    нужен только здесь — списком он уезжал килобайтами в каждой строке)."""
    item = await repo.get(FeedItem, id)  # чужой id → None
    if item is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    try:
        messages = json.loads(item.raw_messages_json or "[]")
    except ValueError:  # битый JSON — пустой список, а не 500 всей вкладки
        messages = []
    return {"feed_item": {**_card(item), "messages": messages}}


@router.get("/api/avatars/{chat_id}")
async def chat_avatar(
    chat_id: int, repo: TenantRepo = Depends(get_tenant_repo)
) -> Response:
    """Аватарка канала: только юзеру, который мониторит этот канал."""
    monitored = (
        await repo.db.scalars(
            repo.query(Monitor).where(Monitor.chat_id == chat_id).limit(1)
        )
    ).first()
    if monitored is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    avatar = await repo.db.get(ChatAvatar, chat_id)
    if avatar is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    return Response(
        content=avatar.image_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": AVATAR_CACHE_CONTROL},
    )


REANALYZE = "reanalyze_feed_item"


@router.delete("/api/feed/{id}")
async def delete_feed_item(
    id: int, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    item = await repo.get(FeedItem, id)  # чужой id → None → 404
    if item is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    await repo.db.delete(item)
    await repo.db.commit()
    return {"status": "deleted", "id": id}


@router.delete("/api/feed")
async def clear_feed(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Очистка ленты — только своей.

    В монолите (server.py:1971) это `DELETE FROM analysis_feed` без условия:
    в мульти-тенанте один клиент стёр бы ленту всему сервису.
    """
    result = await repo.db.execute(
        delete(FeedItem).where(FeedItem.user_id == repo.user_id)
    )
    await repo.db.commit()
    return {"status": "cleared", "removed": deleted_count(result)}


@router.post("/api/feed/{id}/reanalyze", status_code=202)
async def reanalyze(id: int, repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Повторный анализ выборки — задачей, а не вызовом LLM внутри запроса.

    В монолите (server.py:1889) OpenRouter вызывался прямо в обработчике с
    таймаутом 45 секунд: всё это время веб-процесс занят одним клиентом.
    Лента и так обновляется живым опросом, поэтому 202 ничего не ломает.
    """
    item = await repo.get(FeedItem, id)
    if item is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    try:
        messages = json.loads(item.raw_messages_json or "[]")
    except ValueError:
        messages = []
    if not messages:
        # 400 сразу, а не задача, которая гарантированно упадёт в воркере
        raise HTTPException(
            status_code=400, detail="В этой задаче нет исходных постов для анализа"
        )
    job = await enqueue_job(
        repo.db,
        user_id=repo.user_id,
        kind=REANALYZE,
        payload={"feed_item_id": item.id},
    )
    return {"status": "queued", "job_id": job.id}
