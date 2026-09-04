"""Каналы мониторинга (К2) — порт /api/monitors/* из server.py.

В монолите (server.py:1277-1527) все шесть эндпоинтов открыты: список, добавление,
правка, удаление, немедленный запуск и сброс дедупликации. Пока они там, живой
деплой невозможен — любой прохожий читает и меняет чужие каналы.

Отличия порта, ради которых задача и существует:

- роутер целиком за require_user, тенантные чтения только через TenantRepo;
  чужой канал даёт 404, не 403 (403 подтверждает, что объект существует);
- адресация по public_id: последовательный первичный ключ выдаёт число чужих
  строк, а public_id к тому же совпадает со старым TEXT-id из SQLite, поэтому
  ссылки переживают перенос данных (задача 1.5);
- запрет дубля канала — В ПРЕДЕЛАХ пользователя. Глобальный означал бы, что
  первый подписавшийся занимает канал для всего сервиса;
- «Запустить сейчас» кладёт задачу в очередь и отвечает 202, а не опрашивает
  Telegram внутри HTTP-запроса, как монолит (server.py:1446). Долгоживущими
  Telethon-клиентами владеет только воркер: второй клиент на том же auth-key
  выбивает пользователя из его собственного аккаунта (AUTH_KEY_DUPLICATED);
- разрешение канала в Telegram — инъектируемая зависимость, как у входа: тест
  подставляет свою и не ходит в сеть.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func

from app.db import TenantRepo, deleted_count
from app.deps import get_tenant_repo, require_user
from app.models import Monitor, SentMessage
from app.services.jobs import enqueue_job
from app.services.journal import add_log
from app.services.tg_auth import get_telegram_auth_client

router = APIRouter(dependencies=[Depends(require_user)])

POLL_MONITOR = "poll_monitor"


class MonitorCreate(BaseModel):
    chat_target: str = Field(min_length=1, max_length=255)
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    limit: int = Field(default=20, ge=1, le=100)
    offset_hours: int = Field(default=24, ge=1, le=8760)
    is_active: bool = True
    prompt: str | None = None


class MonitorUpdate(BaseModel):
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    limit: int | None = Field(default=None, ge=1, le=100)
    offset_hours: int | None = Field(default=None, ge=1, le=8760)
    is_active: bool | None = None
    prompt: str | None = None


def clean_target(target: str) -> str | int:
    """@name / https://t.me/name / -100... → то, что понимает get_entity.

    Порт server.py:666 без изменений поведения.
    """
    target = target.strip()
    if "t.me/" in target:
        target = target.split("t.me/")[-1].replace("+", "").replace("/", "")
    if target.startswith("@"):
        target = target[1:]
    if target.startswith("-") or target.isdigit():
        try:
            return int(target)
        except ValueError:
            pass
    return target


async def get_entity_resolver(client=Depends(get_telegram_auth_client)):
    """Разрешение канала в сущность Telegram.

    Отдельная зависимость по двум причинам: тест подставляет свою и не ходит
    в сеть, а веб держит клиент ровно на время запроса — долгоживущие клиенты
    принадлежат воркеру.
    """

    async def resolve(target: str):
        return await client.get_entity(clean_target(target))

    return resolve


def _card(m: Monitor, sent_count: int = 0) -> dict[str, Any]:
    """Форма ответа сохраняет имена полей монолита (`limit`, не `limit_count`):
    фронтенд переносится отдельно, ломать его этим переносом незачем."""
    return {
        "public_id": m.public_id,
        "chat_target": m.chat_target,
        "chat_title": m.chat_title,
        "chat_username": m.chat_username,
        "chat_id": m.chat_id,
        "interval_minutes": m.interval_minutes,
        "limit": m.limit_count,
        "offset_hours": m.offset_hours,
        "is_active": m.is_active,
        "prompt": m.prompt,
        "last_checked": m.last_checked,
        "last_sent_message_id": m.last_sent_message_id,
        "sent_count": sent_count,
        "created_at": m.created_at,
    }


async def _sent_counts(repo: TenantRepo, chat_ids: list[int]) -> dict[int, int]:
    """Сколько постов уже отправлено по каждому каналу — одним запросом.

    Монолит спрашивал это в цикле по каналам (server.py:419): на двадцати
    каналах двадцать отдельных запросов на каждую отрисовку списка.
    """
    if not chat_ids:
        return {}
    stmt = (
        repo.query(SentMessage)
        .with_only_columns(SentMessage.chat_id, func.count())
        .where(SentMessage.chat_id.in_(chat_ids))
        .group_by(SentMessage.chat_id)
    )
    return {chat_id: count for chat_id, count in (await repo.db.execute(stmt)).all()}


async def _get_or_404(repo: TenantRepo, public_id: str) -> Monitor:
    monitor = (
        await repo.db.scalars(
            repo.query(Monitor).where(Monitor.public_id == public_id).limit(1)
        )
    ).first()
    if monitor is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    return monitor


@router.get("/api/monitors")
async def list_monitors(
    limit: int = Query(200, ge=1, le=500),
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    monitors = (
        await repo.db.scalars(
            repo.query(Monitor).order_by(Monitor.id.desc()).limit(limit)
        )
    ).all()
    counts = await _sent_counts(repo, [m.chat_id for m in monitors if m.chat_id])
    cards = [_card(m, counts.get(m.chat_id or 0, 0)) for m in monitors]
    return {"total": len(cards), "monitors": cards}


@router.post("/api/monitors", status_code=201)
async def add_monitor(
    req: MonitorCreate,
    repo: TenantRepo = Depends(get_tenant_repo),
    resolve=Depends(get_entity_resolver),
) -> dict:
    try:
        entity = await resolve(req.chat_target)
    except HTTPException:
        raise
    except Exception as exc:  # канал не найден/нет доступа — это 400, не 500
        raise HTTPException(
            status_code=400, detail=f"Канал не найден или недоступен: {exc}"
        ) from exc

    chat_id = getattr(entity, "id", None)
    title = getattr(entity, "title", None) or str(req.chat_target)
    username = getattr(entity, "username", None)

    existing = (
        await repo.db.scalars(
            repo.query(Monitor).where(Monitor.chat_id == chat_id).limit(1)
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=400, detail="Этот канал уже добавлен")

    monitor = Monitor(
        public_id=uuid.uuid4().hex[:8],
        user_id=repo.user_id,
        chat_target=req.chat_target,
        chat_title=title,
        chat_username=username,
        chat_id=chat_id,
        interval_minutes=req.interval_minutes,
        limit_count=req.limit,
        offset_hours=req.offset_hours,
        is_active=req.is_active,
        last_sent_message_id=0,
        prompt=(req.prompt or "").strip() or None,
    )
    repo.db.add(monitor)
    await repo.db.commit()
    await repo.db.refresh(monitor)

    await add_log(
        repo.db,
        repo.user_id,
        "CHANNEL_ADDED",
        f"Добавлен канал '{title}' ({req.chat_target})",
        "SUCCESS",
        chat_id=chat_id,
        chat_title=title,
    )
    return _card(monitor)


@router.patch("/api/monitors/{public_id}")
async def update_monitor(
    public_id: str,
    req: MonitorUpdate,
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    monitor = await _get_or_404(repo, public_id)

    if req.interval_minutes is not None:
        monitor.interval_minutes = req.interval_minutes
    if req.limit is not None:
        monitor.limit_count = req.limit
    if req.offset_hours is not None:
        monitor.offset_hours = req.offset_hours
    if req.is_active is not None:
        monitor.is_active = req.is_active
    if req.prompt is not None:
        monitor.prompt = req.prompt.strip() or None

    await repo.db.commit()
    await repo.db.refresh(monitor)
    return _card(monitor)


@router.delete("/api/monitors/{public_id}")
async def delete_monitor(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    monitor = await _get_or_404(repo, public_id)
    title = monitor.chat_title
    await repo.db.delete(monitor)
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        "CHANNEL_REMOVED",
        f"Удалён канал '{title}'",
        "SUCCESS",
        chat_title=title,
    )
    return {"status": "deleted", "public_id": public_id}


@router.post("/api/monitors/{public_id}/run", status_code=202)
async def run_monitor(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    """Ставит опрос в очередь. 202, а не 200: работа ещё не сделана.

    Монолит опрашивал Telegram прямо здесь и держал HTTP-запрос всё это время.
    """
    monitor = await _get_or_404(repo, public_id)
    job = await enqueue_job(
        repo.db,
        user_id=repo.user_id,
        kind=POLL_MONITOR,
        payload={"monitor_public_id": monitor.public_id},
    )
    return {"status": "queued", "job_id": job.id, "public_id": monitor.public_id}


@router.post("/api/monitors/{public_id}/reset-dedup")
async def reset_dedup(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    """Сброс истории отправленных постов канала — только своей.

    Удаление идёт по паре (user_id, chat_id): без user_id этот эндпоинт стёр бы
    дедупликацию всем, кто следит за тем же каналом, и им прилетели бы сотни
    старых постов повторно.
    """
    monitor = await _get_or_404(repo, public_id)
    result = await repo.db.execute(
        delete(SentMessage).where(
            SentMessage.user_id == repo.user_id,
            SentMessage.chat_id == monitor.chat_id,
        )
    )
    await repo.db.commit()
    removed = deleted_count(result)
    await add_log(
        repo.db,
        repo.user_id,
        "DEDUP_RESET",
        f"Сброшена история отправленных постов канала '{monitor.chat_title}' "
        f"({removed} записей)",
        "SUCCESS",
        chat_id=monitor.chat_id,
        chat_title=monitor.chat_title,
    )
    return {"status": "reset", "removed": removed}


__all__ = ["router", "get_entity_resolver", "clean_target", "POLL_MONITOR"]
