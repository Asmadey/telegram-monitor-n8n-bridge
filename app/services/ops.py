"""Состояние сервиса: база, воркер, очередь (задача 10.2).

`GET /health` отвечает «процесс жив» и ничего не знает ни о базе, ни о
воркере — а это разные процессы. Молчащий воркер снаружи выглядит как
«ничего не происходит»: лента пуста, задача висит в `pending`, ошибок нет
нигде. Три числа ниже — ровно то, чего не хватало, чтобы отличить «сервис
работает» от «сервис отвечает».
"""

import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, WorkerHeartbeat

# Тик воркера — 30 секунд. Порог с запасом на медленный тик и на паузу
# между сменой лидера: одиночный пропуск не должен выглядеть отказом.
WORKER_STALE_AFTER = 180.0
WORKER_NAME = "worker"


def _aware(value: datetime.datetime) -> datetime.datetime:
    """SQLite отдаёт время без зоны, Postgres — с зоной."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


async def record_heartbeat(db: AsyncSession, name: str, *, leader: bool) -> None:
    """Отметиться. Вызывается в тике, а не при старте."""
    row = (
        await db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.name == name))
    ).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    if row is None:
        db.add(WorkerHeartbeat(name=name, beat_at=now, leader=leader))
    else:
        row.beat_at = now
        row.leader = leader
    await db.flush()


async def ops_status(db: AsyncSession) -> dict:
    """Состояние сервиса. Только состояние: ни адресов, ни ключей, ни
    версий — это диагностика, а не паспорт устройства."""
    database = {"ok": True}
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — недоступная база и есть ответ проверки
        database = {"ok": False}

    beat = (
        await db.scalars(
            select(WorkerHeartbeat).where(WorkerHeartbeat.name == WORKER_NAME)
        )
    ).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    worker: dict[str, object]
    if beat is None:
        worker = {
            "seen": False,
            "alive": False,
            "seconds_since_beat": None,
            "leader": False,
        }
    else:
        since = (now - _aware(beat.beat_at)).total_seconds()
        worker = {
            "seen": True,
            "alive": since <= WORKER_STALE_AFTER,
            "seconds_since_beat": round(since),
            "leader": bool(beat.leader),
        }

    pending = (
        await db.scalar(
            select(func.count()).select_from(Job).where(Job.status == "pending")
        )
    ) or 0
    oldest = await db.scalar(
        select(func.min(Job.created_at)).where(Job.status == "pending")
    )
    jobs = {
        "pending": int(pending),
        "oldest_pending_seconds": (
            round((now - _aware(oldest)).total_seconds())
            if oldest is not None
            else None
        ),
    }

    return {"database": database, "worker": worker, "jobs": jobs}
