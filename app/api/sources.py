"""Источники: задача поиска и её каналы (задача 11.6).

Источник отвечает на вопрос «что ищем», канал — «где ищем». До Фазы 11 это
была одна строка, и пять каналов с общими критериями собрать было нечем.

Потолки здесь не аккуратность, а деньги и время: каждый канал — отдельный
запрос к модели, каждый символ промпта уходит в него целиком. Без потолков
прогон растягивается на часы, а счёт не ограничен ничем.

Запуск идемпотентен НА СЕРВЕРЕ. Кнопка в интерфейсе про чужой сеанс ничего
не знает, а два тапа означают двойной счёт за токены и две доставки одного
и того же.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.db import TenantRepo, deleted_count
from app.deps import get_tenant_repo, require_user
from app.models import Job, Monitor, MonitorChannel, SentMessage
from app.services.jobs import STATUS_DONE, STATUS_FAILED
from app.services.journal import add_log
from app.services.stopwords import MAX_STOP_WORDS, parse_stop_words

router = APIRouter(dependencies=[Depends(require_user)])

# Каждый канал — отдельный запрос к модели: десять уже заметно и по времени,
# и по счёту. Потолок назван в тексте отказа, чтобы не гадать.
MAX_CHANNELS = 10
MAX_PROMPT_CHARS = 8000
KIND_POLL = "poll_monitor"


class SourceCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    interval_minutes: int = Field(default=60, ge=5, le=10080)
    answer_prompt: str = ""
    stop_words: str = ""
    is_active: bool = True


class SourceUpdate(BaseModel):
    title: str | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    answer_prompt: str | None = None
    stop_words: str | None = None
    is_active: bool | None = None


class ChannelCreate(BaseModel):
    chat_target: str = Field(min_length=1, max_length=255)
    limit: int = Field(default=20, ge=1, le=200)
    offset_hours: int = Field(default=24, ge=1, le=720)
    extract_prompt: str = ""


class ChannelUpdate(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200)
    offset_hours: int | None = Field(default=None, ge=1, le=720)
    extract_prompt: str | None = None
    is_active: bool | None = None
    position: int | None = Field(default=None, ge=0, le=MAX_CHANNELS)


def clean_target(target: str) -> str | int:
    """@name / https://t.me/name / -100... → то, что понимает get_entity.

    Переехало из `app/api/monitors.py` вместе со снятием того модуля
    (11.8): два API над одной таблицей неизбежно расходятся, и это уже
    случилось однажды.
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


def _check_prompt(text: str | None, what: str) -> str:
    value = (text or "").strip()
    if len(value) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"{what} длиннее {MAX_PROMPT_CHARS} символов: каждый символ "
            "уходит в модель при каждом прогоне",
        )
    return value


def _check_stop_words(text: str | None) -> str:
    value = text or ""
    if len(parse_stop_words(value)) > MAX_STOP_WORDS:
        raise HTTPException(
            status_code=400,
            detail=f"Стоп-слов больше {MAX_STOP_WORDS}: каждое проверяется на "
            "каждом посте каждого канала",
        )
    return value


def _channel_card(channel: MonitorChannel, sent_count: int = 0) -> dict[str, Any]:
    return {
        "channel_id": channel.id,
        "chat_target": channel.chat_target,
        "chat_title": channel.chat_title,
        "chat_username": channel.chat_username,
        "chat_id": channel.chat_id,
        "limit": channel.limit_count,
        "offset_hours": channel.offset_hours,
        "extract_prompt": channel.extract_prompt,
        "position": channel.position,
        "is_active": channel.is_active,
        "last_checked": channel.last_checked,
        "fail_streak": channel.fail_streak,
        "sent_count": sent_count,
    }


def _source_card(
    source: Monitor, channels: list[MonitorChannel], counts: dict[int, int]
) -> dict[str, Any]:
    return {
        "public_id": source.public_id,
        "title": source.title,
        "interval_minutes": source.interval_minutes,
        "answer_prompt": source.answer_prompt,
        "stop_words": source.stop_words,
        "is_active": source.is_active,
        "running": source.running,
        "last_run_at": source.last_run_at,
        "created_at": source.created_at,
        "channels": [
            _channel_card(c, counts.get(c.chat_id or 0, 0))
            for c in sorted(channels, key=lambda c: (c.position, c.id))
        ],
    }


async def _get_or_404(repo: TenantRepo, public_id: str) -> Monitor:
    source = (
        await repo.db.scalars(repo.query(Monitor).where(Monitor.public_id == public_id))
    ).first()
    if source is None:
        # 404, а не 403: 403 подтверждает существование объекта
        raise HTTPException(status_code=404, detail="Источник не найден")
    return source


async def _channels_of(repo: TenantRepo, source_id: int) -> list[MonitorChannel]:
    return list(
        await repo.db.scalars(
            select(MonitorChannel)
            .where(MonitorChannel.monitor_id == source_id)
            .order_by(MonitorChannel.position, MonitorChannel.id)
        )
    )


async def _sent_counts(repo: TenantRepo, chat_ids: list[int]) -> dict[int, int]:
    if not chat_ids:
        return {}
    stmt = (
        repo.query(SentMessage)
        .with_only_columns(SentMessage.chat_id, func.count())
        .where(SentMessage.chat_id.in_(chat_ids))
        .group_by(SentMessage.chat_id)
    )
    return {chat_id: count for chat_id, count in (await repo.db.execute(stmt)).all()}


@router.get("/api/sources")
async def list_sources(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    sources = list(await repo.db.scalars(repo.query(Monitor).order_by(Monitor.id)))
    channels = list(
        await repo.db.scalars(
            select(MonitorChannel)
            .where(MonitorChannel.user_id == repo.user_id)
            .order_by(MonitorChannel.position, MonitorChannel.id)
        )
    )
    counts = await _sent_counts(repo, [c.chat_id for c in channels if c.chat_id])
    by_source: dict[int, list[MonitorChannel]] = {}
    for channel in channels:
        by_source.setdefault(channel.monitor_id, []).append(channel)
    return {
        "sources": [_source_card(s, by_source.get(s.id, []), counts) for s in sources]
    }


@router.post("/api/sources", status_code=201)
async def create_source(
    req: SourceCreate, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = Monitor(
        public_id=uuid.uuid4().hex[:8],
        user_id=repo.user_id,
        title=req.title.strip(),
        interval_minutes=req.interval_minutes,
        answer_prompt=_check_prompt(req.answer_prompt, "Промпт оформления"),
        stop_words=_check_stop_words(req.stop_words),
        is_active=req.is_active,
    )
    repo.db.add(source)
    await repo.db.commit()
    await repo.db.refresh(source)
    await add_log(
        repo.db,
        repo.user_id,
        "SOURCE_ADDED",
        f"Создан источник «{source.title}»",
        "SUCCESS",
        chat_title=source.title,
    )
    return _source_card(source, [], {})


@router.patch("/api/sources/{public_id}")
async def update_source(
    public_id: str, req: SourceUpdate, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = await _get_or_404(repo, public_id)
    if req.title is not None:
        source.title = req.title.strip()
    if req.interval_minutes is not None:
        source.interval_minutes = req.interval_minutes
    if req.answer_prompt is not None:
        source.answer_prompt = _check_prompt(req.answer_prompt, "Промпт оформления")
    if req.stop_words is not None:
        source.stop_words = _check_stop_words(req.stop_words)
    if req.is_active is not None:
        source.is_active = req.is_active
    await repo.db.commit()
    channels = await _channels_of(repo, source.id)
    counts = await _sent_counts(repo, [c.chat_id for c in channels if c.chat_id])
    return _source_card(source, channels, counts)


@router.delete("/api/sources/{public_id}")
async def delete_source(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = await _get_or_404(repo, public_id)
    title = source.title
    # Каналы уезжают каскадом; лента и история дедупликации остаются
    # (ON DELETE SET NULL, задача 11.1): каскад по ним означал бы повторную
    # заливку старых постов пересозданным источником.
    await repo.db.delete(source)
    await repo.db.commit()
    await add_log(
        repo.db,
        repo.user_id,
        "SOURCE_DELETED",
        f"Удалён источник «{title}»",
        "SUCCESS",
        chat_title=title,
    )
    return {"status": "deleted", "public_id": public_id}


@router.post("/api/sources/{public_id}/channels", status_code=201)
async def add_channel(
    public_id: str,
    req: ChannelCreate,
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    """Канал добавляется по ссылке; `chat_id` появится при первом опросе.

    Разрешать канал прямо здесь значило бы требовать живого подключения к
    Telegram на КАЖДОЕ добавление — и зависимость, которая его добывает,
    отвечает 400 раньше, чем эндпоинт успевает проверить владельца: на
    чужой источник приходило бы «аккаунт не подключён» вместо 404.

    Конвейер разрешает канал сам (`chat_id` → ссылка → обновить оба), а
    неудача становится видимым исходом с названной причиной, а не
    отказом на полпути добавления.
    """
    source = await _get_or_404(repo, public_id)
    existing = await _channels_of(repo, source.id)
    if len(existing) >= MAX_CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"В источнике уже {MAX_CHANNELS} каналов — это потолок: "
            "каждый канал стоит отдельного запроса к модели",
        )
    prompt = _check_prompt(req.extract_prompt, "Промпт извлечения")
    target = req.chat_target.strip()

    if any(c.chat_target == target for c in existing):
        raise HTTPException(
            status_code=409,
            detail="Этот канал уже есть в источнике: он опрашивался бы дважды",
        )

    channel = MonitorChannel(
        monitor_id=source.id,
        user_id=repo.user_id,
        chat_target=target,
        chat_title=target.lstrip("@"),
        chat_username=None,
        chat_id=None,
        limit_count=req.limit,
        offset_hours=req.offset_hours,
        extract_prompt=prompt,
        position=len(existing),
    )
    repo.db.add(channel)
    await repo.db.commit()
    await repo.db.refresh(channel)
    await add_log(
        repo.db,
        repo.user_id,
        "CHANNEL_ADDED",
        f"В источник «{source.title}» добавлен канал {channel.chat_title}",
        "SUCCESS",
        chat_title=channel.chat_title,
    )
    return _channel_card(channel)


@router.patch("/api/sources/{public_id}/channels/{channel_id}")
async def update_channel(
    public_id: str,
    channel_id: int,
    req: ChannelUpdate,
    repo: TenantRepo = Depends(get_tenant_repo),
) -> dict:
    source = await _get_or_404(repo, public_id)
    channel = (
        await repo.db.scalars(
            select(MonitorChannel).where(
                MonitorChannel.id == channel_id,
                MonitorChannel.monitor_id == source.id,
            )
        )
    ).first()
    if channel is None:
        raise HTTPException(status_code=404, detail="Канал не найден в источнике")
    if req.limit is not None:
        channel.limit_count = req.limit
    if req.offset_hours is not None:
        channel.offset_hours = req.offset_hours
    if req.extract_prompt is not None:
        channel.extract_prompt = _check_prompt(req.extract_prompt, "Промпт извлечения")
    if req.is_active is not None:
        channel.is_active = req.is_active
        if req.is_active:
            # Возврат в строй сбрасывает счётчик отказов: иначе канал,
            # включённый после починки, отключится на первой же осечке.
            channel.fail_streak = 0
    if req.position is not None:
        channel.position = req.position
    await repo.db.commit()
    return _channel_card(channel)


@router.delete("/api/sources/{public_id}/channels/{channel_id}")
async def remove_channel(
    public_id: str, channel_id: int, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = await _get_or_404(repo, public_id)
    result = await repo.db.execute(
        delete(MonitorChannel).where(
            MonitorChannel.id == channel_id, MonitorChannel.monitor_id == source.id
        )
    )
    await repo.db.commit()
    if not deleted_count(result):
        raise HTTPException(status_code=404, detail="Канал не найден в источнике")
    return {"status": "removed", "channel_id": channel_id}


async def _live_job(repo: TenantRepo, public_id: str) -> Job | None:
    """Задача опроса этого источника, которая ещё не завершилась."""
    jobs = await repo.db.scalars(
        repo.query(Job)
        .where(Job.kind == KIND_POLL, Job.status.not_in([STATUS_DONE, STATUS_FAILED]))
        .order_by(Job.id.desc())
    )
    for job in jobs:
        if f'"{public_id}"' in (job.payload_json or ""):
            return job
    return None


@router.post("/api/sources/{public_id}/run", status_code=202)
async def run_source(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = await _get_or_404(repo, public_id)
    live = await _live_job(repo, source.public_id)
    if live is not None:
        # Идемпотентность на сервере: кнопка про чужой сеанс не знает, а два
        # прогона — это двойной счёт за токены и две доставки одного и того же
        return {"status": "queued", "job_id": live.id, "public_id": public_id}

    from app.services.jobs import enqueue_job

    job = await enqueue_job(
        repo.db,
        user_id=repo.user_id,
        kind=KIND_POLL,
        payload={"monitor_public_id": source.public_id},
    )
    return {"status": "queued", "job_id": job.id, "public_id": public_id}


@router.post("/api/sources/{public_id}/reset-dedup")
async def reset_dedup(
    public_id: str,
    repo: TenantRepo = Depends(get_tenant_repo),
    channel_id: int | None = None,
) -> dict:
    """Забыть, какие посты уже прочитаны, и разобрать их заново.

    Единственный законный способ применить улучшенный промпт к тому, что
    уже прочитано: в обычном ходе дел пост разбирается один раз за жизнь
    источника.

    Удаление идёт в разрезе ИСТОЧНИКА: соседний источник, следящий за тем
    же каналом, свою историю сохраняет — иначе ему прилетели бы сотни
    старых постов повторно.
    """
    source = await _get_or_404(repo, public_id)
    channels = await _channels_of(repo, source.id)
    if channel_id is not None:
        channels = [c for c in channels if c.id == channel_id]
        if not channels:
            raise HTTPException(status_code=404, detail="Канал не найден в источнике")

    condition = SentMessage.monitor_id == source.id
    if channel_id is not None:
        condition = condition & (SentMessage.chat_id == channels[0].chat_id)
    result = await repo.db.execute(delete(SentMessage).where(condition))
    await repo.db.commit()
    removed = deleted_count(result)
    await add_log(
        repo.db,
        repo.user_id,
        "DEDUP_RESET",
        f"Сброшена история прочитанных постов источника «{source.title}» "
        f"({removed} записей)",
        "SUCCESS",
        chat_title=source.title,
    )
    return {"status": "reset", "removed": removed}


@router.get("/api/sources/{public_id}/status")
async def source_status(
    public_id: str, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    source = await _get_or_404(repo, public_id)
    channels = await _channels_of(repo, source.id)
    live = await _live_job(repo, source.public_id)
    return {
        "public_id": source.public_id,
        "title": source.title,
        "running": live is not None or bool(source.running),
        "job_id": live.id if live is not None else None,
        "last_run_at": source.last_run_at,
        "channels": [
            {
                "channel_id": c.id,
                "chat_target": c.chat_target,
                "chat_title": c.chat_title,
                "is_active": c.is_active,
                "last_checked": c.last_checked,
                "fail_streak": c.fail_streak,
            }
            for c in channels
        ],
    }
