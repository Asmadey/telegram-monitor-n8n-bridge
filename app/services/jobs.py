"""Очередь jobs для ручного запуска (задача 4.3 PLAN.md).

Принцип «одна база на всё» (аналог Solid Queue в Rails-шаблоне), Redis
не нужен: web пишет строку (POST /api/monitors/{id}/run, 202 + job_id —
эндпоинты приходят с роутерами Фазы 5), воркер забирает, фронтенд
опрашивает статус.

Захват атомарен ОДНИМ запросом — UPDATE с подзапросом старейшей
pending-строки + RETURNING:

    UPDATE jobs SET status='running', started_at=...
    WHERE id = (SELECT id FROM jobs WHERE status='pending'
                ORDER BY created_at, id LIMIT 1
                FOR UPDATE SKIP LOCKED)          -- только Postgres
    RETURNING id

На Postgres подзапрос блокирует строку и ПРОПУСКАЕТ уже заблокиро-
ванные конкурентами (SKIP LOCKED, буква плана): два воркера физически
не возьмут одну задачу. На SQLite FOR UPDATE не существует — там
одновременность держит сам однозапросный UPDATE (единственный писатель
в моменте), что и гоняет поведенческий тест.

Зависшая задача: running дольше 10 минут (процесс умер посреди) →
обратно в pending. done/failed НЕ возвращаются — повторный запуск
выполненной задачи это повторная отправка вебхуков.
"""

import datetime
import json

from sqlalchemy import select, update

from app.models import Job

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

HUNG_TIMEOUT = 600.0  # 10 минут (план 4.3): running дольше — зависла


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def enqueue_job(
    db, *, user_id: int, kind: str, payload: dict | None = None
) -> Job:
    """Пишет строку в очередь (ручной запуск из UI)."""
    job = Job(
        user_id=user_id,
        kind=kind,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.add(job)
    await db.commit()
    return job


def claim_statement(dialect_name: str, now: datetime.datetime):
    """Атомарный захват старейшей pending-задачи: один UPDATE.

    Возвращает id ровно одной строки ИЛИ ничего — конкурент, забравший
    строку раньше, невидим (SKIP LOCKED на Postgres; на SQLite UPDATE
    единственного писателя в моменте добивается того же).
    """
    subquery = (
        select(Job.id)
        .where(Job.status == STATUS_PENDING)
        .order_by(Job.created_at, Job.id)
        .limit(1)
    )
    if dialect_name == "postgresql":
        subquery = subquery.with_for_update(skip_locked=True)
    return (
        update(Job)
        .where(Job.id == subquery.scalar_subquery())
        .values(status=STATUS_RUNNING, started_at=now)
        .returning(Job.id)
    )


async def claim_next_job(db, *, now: datetime.datetime | None = None):
    """Забрать одну задачу из очереди (вызывает воркер) — или None."""
    now = now or _utcnow()
    result = await db.execute(claim_statement(db.bind.dialect.name, now))
    job_id = result.scalar_one_or_none()
    await db.commit()
    if job_id is None:
        return None
    return await db.get(Job, job_id)


async def requeue_hung_jobs(
    db, *, now: datetime.datetime | None = None, timeout: float = HUNG_TIMEOUT
) -> int:
    """Вернуть зависшие running (started_at старше timeout) в pending.
    Возвращает количество возвращённых; done/failed не трогает."""
    now = now or _utcnow()
    stale_before = now - datetime.timedelta(seconds=timeout)
    result = await db.execute(
        update(Job)
        .where(Job.status == STATUS_RUNNING, Job.started_at < stale_before)
        .values(status=STATUS_PENDING, started_at=None)
    )
    requeued = result.rowcount
    await db.commit()
    return requeued


async def finish_job(db, job: Job) -> None:
    """Успешное завершение (вызывает воркер)."""
    job.status = STATUS_DONE
    job.finished_at = _utcnow()
    await db.commit()


async def fail_job(db, job: Job, *, error: str) -> None:
    """Падение задачи: failed + текст ошибки (диагностика в UI)."""
    job.status = STATUS_FAILED
    job.finished_at = _utcnow()
    job.error = error
    await db.commit()
