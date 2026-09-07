"""Recovery scenarios: real database/worker/dispatch, simulated external services."""

import asyncio
import datetime
import json

import pytest
from sqlalchemy import select
from telethon import errors
from test_57_dispatch import _enable_all
from test_58_worker_body import FakeTelegram, _monitor, _worker

from app.models import FeedItem, Job, LogEntry, Monitor, TelegramAccount
from app.services.dispatch import dispatch


@pytest.mark.asyncio
async def test_failed_analysis_survives_restart_without_refetch(db, user):
    await _monitor(db, user)
    calls = []

    async def broken(*args, **kwargs):
        calls.append("failed")
        raise RuntimeError("provider unavailable")

    worker = _worker(db, dispatcher=broken)
    await worker.run_schedule(db)
    jobs = list(await db.scalars(select(Job)))
    assert any(j.status == "pending" for j in jobs), (
        "raw batch must survive provider outage"
    )

    async def healthy(*args, **kwargs):
        calls.append("recovered")
        return {"status": "dispatched"}

    for job in jobs:
        job.retry_after = None
    await db.commit()
    restarted = _worker(db, dispatcher=healthy, telegram=FakeTelegram(no_account=True))
    await restarted.run_jobs(db)
    assert calls == ["failed", "recovered"]
    assert all(j.status == "done" for j in await db.scalars(select(Job)))


@pytest.mark.asyncio
async def test_successful_analysis_is_saved_before_delivery_and_reused(db, user):
    await _enable_all(db, user.id)
    await _monitor(db, user)
    analyses, deliveries = [], []

    async def llm(payload):
        analyses.append(payload)
        return ("result", 10)

    async def bot(*args):
        return True

    async def webhook(url, body):
        deliveries.append(body)
        assert list(await db.scalars(select(FeedItem))), (
            "save analysis before network send"
        )
        if len(deliveries) == 1:
            raise RuntimeError("temporary outage")

    async def deliver(db, uid, payload, **kwargs):
        return await dispatch(
            db,
            uid,
            payload,
            llm_caller=llm,
            bot_sender=bot,
            webhook_sender=webhook,
            **kwargs,
        )

    worker = _worker(db, dispatcher=deliver)
    await worker.run_schedule(db)
    for job in await db.scalars(select(Job)):
        job.retry_after = None
    await db.commit()
    await _worker(db, dispatcher=deliver).run_jobs(db)
    assert len(deliveries) == 2, "failed delivery must retry"
    assert len(analyses) == 1, "durable successful analysis must not rerun"
    assert deliveries[0]["job_id"] == deliveries[1]["job_id"]
    assert len(list(await db.scalars(select(FeedItem)))) == 1


@pytest.mark.asyncio
async def test_first_monitor_failure_does_not_expire_second(db, user):
    await _monitor(db, user, public_id="one")
    await _monitor(db, user, public_id="two")
    worker = _worker(db)
    seen = []

    async def poll(db, monitor):
        seen.append(monitor.public_id)
        if len(seen) == 1:
            raise RuntimeError("first fails")

    worker.poll_monitor = poll
    error = None
    try:
        await worker.run_schedule(db)
    except Exception as exc:
        error = type(exc).__name__
    assert error is None, f"scheduler stopped: {error}"
    assert seen == ["one", "two"]


@pytest.mark.asyncio
async def test_flood_wait_persists_on_account_across_worker_restart(db, user):
    from app.security.crypto import encrypt

    await _monitor(db, user)
    account = TelegramAccount(
        user_id=user.id,
        phone="test",
        tg_user_id=user.id,
        session_string_encrypted=encrypt("test session"),
    )
    db.add(account)
    await db.commit()
    worker = _worker(
        db,
        telegram=FakeTelegram(fail=errors.FloodWaitError(request=None, capture=3600)),
    )
    await worker.run_schedule(db)
    await db.refresh(account)
    assert account.retry_after is not None
    assert "retry_after" not in Monitor.__table__.columns, (
        "FloodWait belongs to the Telegram auth-key, not one monitor"
    )


@pytest.mark.asyncio
async def test_full_selected_text_reaches_model(db, user):
    await _enable_all(db, user.id)
    texts = [" leading and trailing ", "a" * 30000, "b" * 30000, "last message"]
    received = []

    async def llm(payload):
        received.extend(json.loads(payload["messages"][1]["content"])["post"])
        return ("result", 10)

    async def sender(*args):
        return True

    await dispatch(
        db,
        user.id,
        {
            "chat_id": -1001,
            "messages": [{"id": i + 1, "text": text} for i, text in enumerate(texts)],
        },
        llm_caller=llm,
        bot_sender=sender,
        webhook_sender=sender,
    )
    assert "".join(item["пост"] for item in received) == "".join(texts)


@pytest.mark.asyncio
async def test_partial_analysis_retry_does_not_reread_successful_chunk(db, user):
    await _enable_all(db, user.id)
    await _monitor(db, user)
    seen = []
    failure = True

    async def llm(payload):
        nonlocal failure
        text = json.loads(payload["messages"][1]["content"])["post"][0]["пост"]
        seen.append(text[0])
        if text.startswith("b") and failure:
            failure = False
            raise RuntimeError("provider outage in second request")
        return ("analysis " + text[0], 1)

    async def sender(*args):
        return True

    async def deliver(db, uid, payload, **kwargs):
        return await dispatch(
            db,
            uid,
            payload,
            llm_caller=llm,
            bot_sender=sender,
            webhook_sender=sender,
            **kwargs,
        )

    worker = _worker(
        db,
        dispatcher=deliver,
        telegram=FakeTelegram(
            posts=[{"id": 11, "text": "a" * 30000}, {"id": 12, "text": "b" * 30000}]
        ),
    )
    await worker.run_schedule(db)
    for job in await db.scalars(select(Job)):
        job.retry_after = None
    await db.commit()
    await _worker(db, dispatcher=deliver).run_jobs(db)
    assert seen == ["a", "b", "b"], "completed chunks must survive restart"


@pytest.mark.asyncio
async def test_monthly_budget_defers_unread_batch(db, user):
    from app.models import LLMUsage, SentMessage
    from app.services.llm import MONTHLY_TOKEN_LIMIT

    await _enable_all(db, user.id)
    await _monitor(db, user)
    db.add(
        LLMUsage(
            user_id=user.id,
            period=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m"),
            tokens=MONTHLY_TOKEN_LIMIT,
        )
    )
    await db.commit()
    called = []

    async def llm(payload):
        called.append(payload)
        return ("should not call", 1)

    async def deliver(db, uid, payload, **kwargs):
        return await dispatch(db, uid, payload, llm_caller=llm, **kwargs)

    await _worker(db, dispatcher=deliver).run_schedule(db)
    assert not called
    assert all(
        not message.processed for message in await db.scalars(select(SentMessage))
    )
    assert any(job.status == "pending" for job in await db.scalars(select(Job)))
    pending = (await db.scalars(select(Job).where(Job.status == "pending"))).first()
    assert pending is not None and pending.retry_after is not None
    retry_after = pending.retry_after.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    assert (retry_after.year, retry_after.month) != (now.year, now.month), (
        "a monthly budget must defer until the next accounting period"
    )


@pytest.mark.asyncio
async def test_retention_keeps_dedup_and_unfinished_analysis(db, user):
    from app.models import SentMessage
    from app.services.cleanup import purge_older_than
    from app.services.dedup import filter_new

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    db.add(
        SentMessage(
            user_id=user.id,
            chat_id=-1001,
            message_id=11,
            sent_at=old,
            text="old private source text",
            processed=True,
        )
    )
    db.add(
        FeedItem(
            user_id=user.id,
            job_id="unfinished",
            created_at=old,
            delivery_status="ANALYZING",
            raw_messages_json="[]",
        )
    )
    await db.commit()
    await purge_older_than(db, user.id, 30)
    assert await filter_new(db, user.id, -1001, [{"id": 11, "text": "again"}]) == []
    assert list(await db.scalars(select(FeedItem)))


@pytest.mark.asyncio
async def test_retention_scrubs_only_finished_payloads(db, user):
    from app.models import SentMessage
    from app.services.cleanup import purge_older_than

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    finished_message = SentMessage(
        user_id=user.id,
        chat_id=-1001,
        message_id=11,
        sent_at=old,
        text="finished private source",
        sender="finished sender",
        reactions_json='[{"emoji":"x"}]',
        processed=True,
    )
    pending_message = SentMessage(
        user_id=user.id,
        chat_id=-1001,
        message_id=12,
        sent_at=old,
        text="pending private source",
        sender="pending sender",
        processed=False,
    )
    successful_feed = FeedItem(
        user_id=user.id,
        job_id="success",
        created_at=old,
        delivery_status="SUCCESS",
        raw_messages_json='[{"text":"safe to remove"}]',
    )
    error_feed = FeedItem(
        user_id=user.id,
        job_id="error",
        created_at=old,
        delivery_status="ERROR",
        raw_messages_json='[{"text":"needed for retry"}]',
    )
    completed_job = Job(
        user_id=user.id,
        kind="process_batch",
        payload_json='{"done":true}',
        status="done",
        finished_at=old,
    )
    pending_job = Job(
        user_id=user.id,
        kind="process_batch",
        payload_json='{"retry":true}',
        status="pending",
        created_at=old,
        error="temporary failure",
    )
    db.add_all(
        [
            finished_message,
            pending_message,
            successful_feed,
            error_feed,
            completed_job,
            pending_job,
        ]
    )
    await db.commit()

    await purge_older_than(db, user.id, 30)

    await db.refresh(finished_message)
    await db.refresh(pending_message)
    assert finished_message.text is None and finished_message.sender is None
    assert pending_message.text == "pending private source"
    assert pending_message.sender == "pending sender"
    feed_by_job = {item.job_id: item for item in await db.scalars(select(FeedItem))}
    assert "success" not in feed_by_job
    assert feed_by_job["error"].raw_messages_json == '[{"text":"needed for retry"}]'
    jobs = {job.payload_json: job for job in await db.scalars(select(Job))}
    assert '{"done":true}' not in jobs
    assert jobs['{"retry":true}'].error == "temporary failure"


@pytest.mark.asyncio
async def test_account_floodwait_blocks_other_monitors_but_not_other_owner(
    db, user_a, user_b
):
    from app.models import TelegramAccount
    from app.security.crypto import encrypt

    for user in (user_a, user_b):
        db.add(
            TelegramAccount(
                user_id=user.id,
                phone="test",
                tg_user_id=user.id,
                session_string_encrypted=encrypt("test session"),
            )
        )
    await db.commit()
    await _monitor(db, user_a, public_id="a1")
    await _monitor(db, user_a, public_id="a2")
    await _monitor(db, user_b, public_id="b1")
    owner_a, owner_b = user_a.id, user_b.id
    calls = []

    class Gateway(FakeTelegram):
        async def client_for(self, db, uid):
            calls.append(uid)
            if uid == owner_a:
                raise errors.FloodWaitError(request=None, capture=3600)
            return object()

    await _worker(db, telegram=Gateway()).run_schedule(db)
    assert calls == [owner_a, owner_b]
    calls.clear()
    await _worker(db, telegram=Gateway()).poll_monitor(db, await db.get(Monitor, 2))
    assert calls == []


@pytest.mark.asyncio
async def test_account_floodwait_blocks_manual_jobs_for_same_auth_key(
    db, user_a, user_b
):
    from app.security.crypto import encrypt
    from app.services.jobs import enqueue_job

    owner_a, owner_b = user_a.id, user_b.id
    for user in (user_a, user_b):
        db.add(
            TelegramAccount(
                user_id=user.id,
                phone="test",
                tg_user_id=user.id,
                session_string_encrypted=encrypt("test session"),
            )
        )
    await db.commit()
    a1 = await _monitor(db, user_a, public_id="manual-a1")
    a2 = await _monitor(db, user_a, public_id="manual-a2")
    b1 = await _monitor(db, user_b, public_id="manual-b1")
    for owner, monitor in ((user_a, a1), (user_a, a2), (user_b, b1)):
        await enqueue_job(
            db,
            user_id=owner.id,
            kind="poll_monitor",
            payload={"monitor_public_id": monitor.public_id},
        )
    calls = []

    class Gateway(FakeTelegram):
        async def client_for(self, db, uid):
            calls.append(uid)
            if uid == owner_a:
                raise errors.FloodWaitError(request=None, capture=3600)
            return object()

    await _worker(db, telegram=Gateway()).run_jobs(db)
    assert calls == [owner_a, owner_b], (
        "the second manual job sharing one auth-key must be blocked"
    )
    manual_jobs = list(await db.scalars(select(Job).where(Job.kind == "poll_monitor")))
    jobs_a = [job for job in manual_jobs if job.user_id == owner_a]
    jobs_b = [job for job in manual_jobs if job.user_id == owner_b]
    assert all(job.status == "pending" for job in jobs_a)
    assert all(job.retry_after is not None for job in jobs_a)
    assert [job.status for job in jobs_b] == ["done"]


@pytest.mark.asyncio
async def test_no_new_messages_restores_skipped_dedup_journal(db, user):
    monitor = await _monitor(db, user)
    worker = _worker(db)
    assert await worker.poll_monitor(db, monitor) == "dispatched"
    assert await worker.poll_monitor(db, monitor) == "no_new"
    entries = list(
        await db.scalars(
            select(LogEntry).where(
                LogEntry.user_id == user.id,
                LogEntry.status == "SKIPPED_DEDUP",
            )
        )
    )
    assert len(entries) == 1
    assert entries[0].messages_count == 0


@pytest.mark.asyncio
async def test_standby_worker_does_not_tick_until_leadership():
    import asyncio

    from test_40_worker import FakePool

    from app.worker import Worker

    acquired = False
    ticks = []

    async def guard():
        return acquired

    async def tick():
        ticks.append(1)

    worker = Worker(pool=FakePool(), tick=tick, tick_interval=0.01)
    worker._leadership = guard
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0.03)
        assert not ticks, "standby must not open clients or consume jobs"
        acquired = True
        await asyncio.sleep(0.03)
        assert ticks
    finally:
        worker.request_stop()
        await task


@pytest.mark.asyncio
async def test_postgres_leadership_waits_then_releases_the_session_lock():
    from app.worker import PostgresLeadership

    class Connection:
        def __init__(self):
            self.try_results = iter((False, True))
            self.statements = []

        async def scalar(self, statement, parameters=None):
            sql = str(statement)
            self.statements.append(sql)
            if "pg_try_advisory_lock" in sql:
                return next(self.try_results)
            if "pg_advisory_unlock" in sql:
                return True
            raise AssertionError(sql)

        async def execute(self, statement, parameters=None):
            self.statements.append(str(statement))

    connection = Connection()
    leadership = PostgresLeadership(connection)
    assert hasattr(leadership, "acquire"), (
        "leadership must be acquired before Worker.run starts"
    )
    assert await leadership.acquire(asyncio.Event(), retry_interval=0)
    assert leadership.acquired
    await leadership.release()
    assert not leadership.acquired
    assert sum("pg_try_advisory_lock" in sql for sql in connection.statements) == 2
    assert any("pg_advisory_unlock" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_live_postgres_leadership_is_exclusive_and_transferable(
    alembic_target_db,
):
    """SQLite cannot prove a PostgreSQL session lock. CI with a real
    TEST_DATABASE_URL verifies that a standby waits and acquires only after
    the active owner releases its dedicated connection lock."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.worker import PostgresLeadership

    engine = create_async_engine(alembic_target_db)
    stop = asyncio.Event()
    try:
        async with engine.connect() as first, engine.connect() as second:
            await first.execution_options(isolation_level="AUTOCOMMIT")
            await second.execution_options(isolation_level="AUTOCOMMIT")
            active = PostgresLeadership(first)
            standby = PostgresLeadership(second)
            assert await active.acquire(stop, retry_interval=0)
            waiting = asyncio.create_task(standby.acquire(stop, retry_interval=0.01))
            await asyncio.sleep(0.05)
            assert not waiting.done(), "two PostgreSQL sessions owned one lock"
            await active.release()
            assert await asyncio.wait_for(waiting, timeout=1)
            await standby.release()
    finally:
        await engine.dispose()
