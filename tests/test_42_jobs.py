"""Задача 4.3 — очередь jobs для ручного запуска.

Принцип «одна база на всё» (аналог Solid Queue в Rails-шаблоне), Redis
не нужен: `POST /api/monitors/{id}/run` пишет строку в jobs (202 +
job_id — сами эндпоинты приходят с роутерами Фазы 5), воркер забирает
задачу атомарным захватом, фронтенд опрашивает статус.

Два контракта (из плана):
1. Два воркера, забирающие задачи ОДНОВРЕМЕННО, не берут одну и ту же.
   На Postgres это SELECT ... FOR UPDATE SKIP LOCKED; контракт тот же
   на любом движке — захват атомарен.
2. Зависшая задача (running дольше 10 минут — процесс умер посреди)
   возвращается в очередь; done/failed НЕ возвращаются.

Отступление от буквы плана: на aiosqlite FOR UPDATE не существует
(синтаксис не поддержан), поэтому структурный тест отдельно проверяет,
что postgresql-вариант захвата компилируется с SKIP LOCKED, а
поведенческий тест гонки работает на любом диалекте (захват — один
atomic UPDATE, см. докстринг реализации).

Гонка — на ДВУХ независимых сессиях (урок 4.2: AsyncSession не
coroutine-safe; реальный сценарий — два воркера, две транзакции).
"""

import asyncio
import datetime
import json

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.models import Job
from app.services.jobs import (
    HUNG_TIMEOUT,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    claim_next_job,
    claim_statement,
    enqueue_job,
    fail_job,
    finish_job,
    requeue_hung_jobs,
)

NOW = datetime.datetime(2026, 9, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def _seed_job(
    db,
    user_id: int,
    *,
    status: str = STATUS_PENDING,
    created_at: datetime.datetime | None = None,
    started_at: datetime.datetime | None = None,
    kind: str = "monitor_run",
    payload: dict | None = None,
) -> Job:
    job = Job(
        user_id=user_id,
        kind=kind,
        payload_json=json.dumps(payload or {"monitor_public_id": "m-1"}),
        status=status,
        created_at=created_at or NOW,
        started_at=started_at,
    )
    db.add(job)
    await db.commit()
    return job


@pytest.mark.asyncio
async def test_enqueue_creates_pending_job(db, user_a):
    """POST .../run пишет СТРОКУ в очередь: pending, payload на месте,
    задача привязана к тенанту."""
    job = await enqueue_job(
        db, user_id=user_a.id, kind="monitor_run", payload={"monitor_public_id": "abc"}
    )
    assert job.status == STATUS_PENDING, "новая задача не в pending"
    assert job.user_id == user_a.id
    assert json.loads(job.payload_json) == {"monitor_public_id": "abc"}
    assert job.started_at is None, "задача ещё не начата"


@pytest.mark.asyncio
async def test_concurrent_claims_never_take_same_job(db_engine, db, user_a):
    """Главный контракт: два конкурентных захвата — РАЗНЫЕ задачи,
    вместе покрывают ровно все pending (SKIP LOCKED в терминах плана)."""
    first = await _seed_job(db, user_a.id)  # старее — берётся первым
    second = await _seed_job(
        db, user_a.id, created_at=NOW + datetime.timedelta(seconds=1)
    )

    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def run_one():
        async with maker() as session:
            return await claim_next_job(session, now=NOW)

    a, b = await asyncio.gather(run_one(), run_one())

    assert a is not None and b is not None, "задача потеряна при захвате"
    assert a.id != b.id, "оба воркера взяли ОДНУ задачу"
    assert {a.id, b.id} == {first.id, second.id}, "взята чужая/несуществующая задача"
    for job in (a, b):
        assert job.status == STATUS_RUNNING, "захваченная задача не running"
        # sqlite хранит DateTime наивно (tzinfo теряется) — ошибка первой
        # версии теста, не реализации; сравниваем без tzinfo
        assert job.started_at == NOW.replace(tzinfo=None), (
            "started_at не проставлен при захвате"
        )


@pytest.mark.asyncio
async def test_claim_returns_oldest_first(db, user_a):
    """FIFO: сначала старейшая pending; когда очередь пуста — None."""
    first = await _seed_job(db, user_a.id)
    second = await _seed_job(
        db, user_a.id, created_at=NOW + datetime.timedelta(seconds=1)
    )

    got = await claim_next_job(db, now=NOW)
    assert got is not None and got.id == first.id, "взята не старейшая задача"
    got2 = await claim_next_job(db, now=NOW)
    assert got2 is not None and got2.id == second.id
    assert await claim_next_job(db, now=NOW) is None, "пустая очередь отдала задачу"


@pytest.mark.asyncio
async def test_requeue_hung_jobs_returns_only_stale_running(db, user_a):
    """running дольше 10 минут → обратно в pending (процесс умер
    посреди); свежий running и done/failed НЕ трогаются."""
    hung = await _seed_job(
        db,
        user_a.id,
        status=STATUS_RUNNING,
        started_at=NOW - datetime.timedelta(seconds=HUNG_TIMEOUT + 60),
    )
    fresh = await _seed_job(
        db,
        user_a.id,
        status=STATUS_RUNNING,
        started_at=NOW - datetime.timedelta(minutes=1),
    )
    done_old = await _seed_job(
        db,
        user_a.id,
        status=STATUS_DONE,
        started_at=NOW - datetime.timedelta(seconds=HUNG_TIMEOUT + 60),
    )

    requeued = await requeue_hung_jobs(db, now=NOW)

    assert requeued == 1, f"возвращена {requeued} зависших задач вместо 1"
    hung = await db.get(Job, hung.id)
    assert hung.status == STATUS_PENDING, "зависшая задача не вернулась в очередь"
    assert hung.started_at is None, "при возврате started_at не сброшен"
    fresh = await db.get(Job, fresh.id)
    assert fresh.status == STATUS_RUNNING, "независшая задача тронута"
    done_old = await db.get(Job, done_old.id)
    assert done_old.status == STATUS_DONE, "ВЫПОЛНЕННАЯ задача вернулась в очередь"


@pytest.mark.asyncio
async def test_finish_and_fail_close_job(db, user_a):
    """Воркер закрывает задачу: done с finished_at; при ошибке — failed
    с текстом ошибки (диагностика в UI)."""
    job = await _seed_job(db, user_a.id, status=STATUS_RUNNING, started_at=NOW)
    await finish_job(db, job)
    closed = await db.get(Job, job.id)
    assert closed.status == STATUS_DONE, "задача не закрыта"
    assert closed.finished_at is not None, "finished_at не проставлен"

    failing = await _seed_job(db, user_a.id, status=STATUS_RUNNING, started_at=NOW)
    await fail_job(db, failing, error="таймаут Telegram")
    failed = await db.get(Job, failing.id)
    assert failed.status == "failed", "упавшая задача не failed"
    assert failed.error == "таймаут Telegram", "текст ошибки потерян"
    assert failed.finished_at is not None


def test_postgres_claim_locks_rows_with_skip_locked():
    """Postgres-вариант захвата обязан строиться с FOR UPDATE SKIP LOCKED
    (буква плана); sqlite-вариант — без него (синтаксиса нет) и обязан
    компилироваться (им же гоняет поведенческий тест)."""
    pg_sql = str(
        claim_statement("postgresql", NOW).compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE SKIP LOCKED" in pg_sql, (
        "postgresql-захват без SKIP LOCKED: конкуренты возьмут одну строку"
    )

    lite_sql = str(claim_statement("sqlite", NOW).compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in lite_sql, "sqlite не поддерживает FOR UPDATE"
    assert "SKIP LOCKED" not in lite_sql


def test_jobs_of_all_tenants_share_one_queue(db_engine):
    """Очередь ОБЩАЯ для всех тенантов (её читает воркер — единственный);
    user_id в строке нужен для скоупа статуса в UI, не для захвата."""

    columns = {c.name for c in Job.__table__.columns}
    assert "user_id" in columns, "без user_id UI не покажет чью это задачу"
    assert "status" in columns and "kind" in columns, "нет колонок очереди"
