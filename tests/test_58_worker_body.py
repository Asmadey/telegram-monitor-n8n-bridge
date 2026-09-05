"""Тело цикла воркера: очередь jobs, расписание, автоочистка.

Каркас процесса закрыт задачей 4.1 (test_40): цикл тикает, переживает
падения, умирает по SIGTERM с закрытым пулом. Тик при этом делал ровно
одну вещь — щётку пула. То есть очередь `jobs` наполнялась из интерфейса
(`POST /api/monitors/{id}/run` честно отвечал 202) и не разбиралась
никогда, мониторы по расписанию не опрашивались, доставка не работала.
Это блокировало живой деплой целиком.

Здесь проверяется тик как единица работы:

1. очередь — захват, выполнение, done/failed, продолжение после падения;
2. расписание — монитор с истёкшим интервалом опрашивается, свежий нет;
3. автоочистка — не чаще раза в сутки, в разрезе тенанта;
4. **тенантность** — это главное. Задача берётся из очереди БЕЗ фильтра по
   `user_id` (воркер обслуживает всех), а всё дальнейшее идёт по `user_id`
   ИЗ СТРОКИ ЗАДАЧИ. Единственное место в проекте с таким источником, и
   ошибка здесь означает чужие посты в чужом вебхуке.

Telegram заменён шлюзом-двойником: живой MTProto в тестах не трогается
(и не может — autouse-страж conftest режет исходящий HTTP).
"""

import datetime
import json

import pytest
from sqlalchemy import select
from telethon import errors

from app.models import (
    ChatAvatar,
    FeedItem,
    Integration,
    Job,
    LogEntry,
    Monitor,
    SentMessage,
)
from app.services.jobs import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    enqueue_job,
)

POLL = "poll_monitor"
REANALYZE = "reanalyze_feed_item"


def _utc(**delta) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)


def _post(msg_id: int) -> dict:
    return {
        "id": msg_id,
        "text": f"пост {msg_id}",
        "post_url": f"https://t.me/channel/{msg_id}",
        "date": _utc(minutes=5).isoformat(),
    }


class FakeEntity:
    def __init__(self, chat_id=-1001, title="Канал", username="channel"):
        self.id = chat_id
        self.title = title
        self.username = username


class FakeTelegram:
    """Шлюз-двойник: даёт клиента, разрешает цель, отдаёт посты и аватарку."""

    def __init__(
        self, *, posts=None, avatar=b"\xff\xd8jpeg", fail=None, no_account=False
    ):
        self.posts = posts if posts is not None else [_post(11), _post(12)]
        # НЕ self.avatar: атрибут затёр бы одноимённый метод шлюза, и вызов
        # падал бы «'bytes' object is not callable» — дефект двойника, а не
        # воркера (пойман красной фазой)
        self.avatar_bytes = avatar
        self.fail = fail
        self.no_account = no_account
        self.fetches: list[str] = []

    async def client_for(self, db, user_id):
        return None if self.no_account else object()

    async def resolve(self, client, target):
        if self.fail is not None:
            raise self.fail
        return FakeEntity()

    async def fetch(self, client, entity, *, limit, offset_hours):
        self.fetches.append(getattr(entity, "username", "?"))
        return list(self.posts)

    async def avatar(self, client, entity):
        return self.avatar_bytes


class RecordingDispatcher:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, user_id, payload, **kwargs):
        self.calls.append({"user_id": user_id, "payload": payload, **kwargs})
        return {"status": "dispatched"}


async def _monitor(db, user, **overrides) -> Monitor:
    fields = {
        "user_id": user.id,
        "chat_target": "@channel",
        "chat_title": "Канал",
        "chat_username": "channel",
        "chat_id": -1001,
        "interval_minutes": 60,
        "limit_count": 20,
        "offset_hours": 24,
        "is_active": True,
        "last_checked": _utc(hours=3),
        "public_id": overrides.pop("public_id", "mon-1"),
    }
    fields.update(overrides)
    monitor = Monitor(**fields)
    db.add(monitor)
    await db.commit()
    return monitor


def _worker(db, *, telegram=None, dispatcher=None, pool=None):
    """Воркер с подменённым сессиймейкером: тик обязан работать в ТОЙ ЖЕ
    сессии, что и посев теста, иначе проверять нечего."""
    from app.worker import Worker

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    return Worker(
        pool=pool or _FakePool(),
        sessionmaker=_Maker(),
        telegram=telegram or FakeTelegram(),
        dispatcher=dispatcher or RecordingDispatcher(),
    )


class _FakePool:
    async def sweep_idle(self) -> int:
        return 0

    async def close(self) -> None:
        pass


async def _jobs(db) -> list[Job]:
    return list(await db.scalars(select(Job)))


# --------------------------------------------------------------------------
# Очередь
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_poll_job_is_executed_and_marked_done(db, user):
    monitor = await _monitor(db, user)
    await enqueue_job(
        db, user_id=user.id, kind=POLL, payload={"monitor_public_id": monitor.public_id}
    )
    dispatcher = RecordingDispatcher()

    await _worker(db, dispatcher=dispatcher)._default_tick()

    assert dispatcher.calls, "задача из очереди не привела к доставке"
    assert dispatcher.calls[0]["user_id"] == user.id
    assert [m["id"] for m in dispatcher.calls[0]["payload"]["messages"]] == [11, 12]
    job = (await _jobs(db))[0]
    assert job.status == STATUS_DONE, f"статус задачи {job.status}, ошибка: {job.error}"


@pytest.mark.asyncio
async def test_dedup_holds_between_ticks(db, user):
    """Второй опрос тех же постов не отправляет ничего: дедупликация — это
    то, ради чего история sent_messages переносилась из SQLite."""
    monitor = await _monitor(db, user)
    telegram, dispatcher = FakeTelegram(), RecordingDispatcher()
    worker = _worker(db, telegram=telegram, dispatcher=dispatcher)

    for _ in range(2):
        await enqueue_job(
            db,
            user_id=user.id,
            kind=POLL,
            payload={"monitor_public_id": monitor.public_id},
        )
        await worker._default_tick()

    assert len(telegram.fetches) == 2, "второй опрос не состоялся"
    assert len(dispatcher.calls) == 1, (
        f"те же посты доставлены {len(dispatcher.calls)} раз — дедупликация не держит"
    )
    saved = list(await db.scalars(select(SentMessage)))
    assert len(saved) == 2


@pytest.mark.asyncio
async def test_job_never_reaches_another_tenants_monitor(db, user_a, user_b):
    """Задача пользователя B с public_id монитора пользователя A не должна
    опросить чужой канал. public_id уникален В ПРЕДЕЛАХ пользователя, так
    что «найти по public_id» без user_id — это чужой ресурс в руках."""
    # монитор A НЕ должен быть просрочен: иначе его законно опросит шаг
    # расписания, и тест засчитает чужой опрос там, где проверяется очередь
    monitor_a = await _monitor(
        db, user_a, public_id="shared-id", last_checked=_utc(minutes=1)
    )
    await enqueue_job(
        db,
        user_id=user_b.id,
        kind=POLL,
        payload={"monitor_public_id": monitor_a.public_id},
    )
    telegram, dispatcher = FakeTelegram(), RecordingDispatcher()

    await _worker(db, telegram=telegram, dispatcher=dispatcher)._default_tick()

    assert not telegram.fetches, "воркер опросил канал ЧУЖОГО пользователя"
    assert not dispatcher.calls, "чужие посты ушли в доставку"
    job = (await _jobs(db))[0]
    assert job.status == STATUS_FAILED, "задача на чужой ресурс считается успешной"


@pytest.mark.asyncio
async def test_failed_job_does_not_stop_the_queue(db, user):
    """Один сломанный канал не должен останавливать остальных: задача
    помечается failed, цикл идёт дальше."""
    good = await _monitor(db, user, public_id="good")
    await enqueue_job(
        db, user_id=user.id, kind=POLL, payload={"monitor_public_id": "нет такого"}
    )
    await enqueue_job(
        db, user_id=user.id, kind=POLL, payload={"monitor_public_id": good.public_id}
    )
    dispatcher = RecordingDispatcher()

    await _worker(db, dispatcher=dispatcher)._default_tick()

    statuses = sorted(job.status for job in await _jobs(db))
    assert statuses == [STATUS_DONE, STATUS_FAILED], statuses
    assert dispatcher.calls, "вторая задача не выполнена после падения первой"


@pytest.mark.asyncio
async def test_job_error_text_is_redacted(db, user):
    """`jobs.error` виден в интерфейсе — это второй сток для секретов после
    журнала (4.6). Текст исключения httpx несёт заголовок Authorization."""
    await _monitor(db, user)
    leaky = RuntimeError("сбой: Authorization: Bearer sk-or-v1-abcdef0123456789")
    await enqueue_job(
        db, user_id=user.id, kind=POLL, payload={"monitor_public_id": "mon-1"}
    )

    await _worker(db, telegram=FakeTelegram(fail=leaky))._default_tick()

    job = (await _jobs(db))[0]
    assert job.status == STATUS_FAILED
    assert "sk-or-v1-abcdef0123456789" not in (job.error or ""), (
        f"ключ утёк в jobs.error: {job.error}"
    )


@pytest.mark.asyncio
async def test_hung_jobs_are_requeued(db, user):
    """Задача, брошенная умершим процессом (running дольше 10 минут),
    возвращается в очередь — иначе она висит навсегда."""
    monitor = await _monitor(db, user)
    job = await enqueue_job(
        db, user_id=user.id, kind=POLL, payload={"monitor_public_id": monitor.public_id}
    )
    job.status = STATUS_RUNNING
    job.started_at = _utc(hours=1)
    await db.commit()

    await _worker(db)._default_tick()

    await db.refresh(job)
    assert job.status == STATUS_DONE, (
        f"зависшая задача не подобрана тиком (статус {job.status})"
    )


@pytest.mark.asyncio
async def test_reanalyze_job_updates_the_feed_item(db, user):
    """Кнопка «переанализировать» (202) обязана чем-то заканчиваться:
    задача перезаписывает ai_analysis существующей записи ленты."""
    item = FeedItem(
        user_id=user.id,
        job_id="feed-1",
        chat_id=-1001,
        chat_title="Канал",
        messages_count=2,
        ai_analysis="старый анализ",
        raw_messages_json=json.dumps([_post(11), _post(12)]),
    )
    db.add(item)
    await db.commit()
    await enqueue_job(
        db, user_id=user.id, kind=REANALYZE, payload={"feed_item_id": item.id}
    )

    async def fake_llm(db_, user_id, messages, **kwargs):
        return "новый анализ"

    worker = _worker(db)
    worker.llm = fake_llm
    await worker._default_tick()

    await db.refresh(item)
    assert item.ai_analysis == "новый анализ", "лента не обновлена задачей"
    assert (await _jobs(db))[0].status == STATUS_DONE


# --------------------------------------------------------------------------
# Расписание
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_due_monitor_is_polled_without_any_job(db, user):
    """Расписание — не очередь: монитор с истёкшим интервалом опрашивается
    сам. Без этого сервис работает только по кнопке."""
    monitor = await _monitor(db, user, interval_minutes=60, last_checked=_utc(hours=2))
    telegram, dispatcher = FakeTelegram(), RecordingDispatcher()

    await _worker(db, telegram=telegram, dispatcher=dispatcher)._default_tick()

    assert telegram.fetches, "монитор с истёкшим интервалом не опрошен"
    await db.refresh(monitor)
    # SQLite возвращает datetime без tzinfo — сравнение naive с aware бросает
    # TypeError независимо от поведения воркера; приводим обе стороны
    checked = monitor.last_checked.replace(tzinfo=datetime.timezone.utc)
    assert checked > _utc(minutes=1), "last_checked не обновлён"


@pytest.mark.asyncio
async def test_fresh_and_inactive_monitors_are_left_alone(db, user):
    """Свежий и выключенный мониторы не опрашиваются: лишний опрос — это
    трафик к Telegram от лица живого аккаунта, то есть риск ограничений."""
    await _monitor(db, user, public_id="fresh", last_checked=_utc(minutes=1))
    await _monitor(
        db, user, public_id="off", is_active=False, last_checked=_utc(days=1)
    )
    telegram = FakeTelegram()

    await _worker(db, telegram=telegram)._default_tick()

    assert not telegram.fetches, f"опрошены лишние мониторы: {telegram.fetches}"


@pytest.mark.asyncio
async def test_never_polled_monitor_runs_immediately(db, user):
    """last_checked = NULL (только что добавленный канал) — опрос сразу."""
    await _monitor(db, user, last_checked=None)
    telegram = FakeTelegram()
    await _worker(db, telegram=telegram)._default_tick()
    assert telegram.fetches, "новый монитор ждёт целый интервал до первого опроса"


@pytest.mark.asyncio
async def test_user_without_telegram_account_is_skipped(db, user):
    """Монитор есть, аккаунт не подключён — тик пропускает пользователя,
    а не падает."""
    await _monitor(db, user, last_checked=None)
    telegram = FakeTelegram(no_account=True)
    await _worker(db, telegram=telegram)._default_tick()
    assert not telegram.fetches


@pytest.mark.asyncio
async def test_flood_wait_does_not_kill_the_tick(db, user):
    """FloodWaitError — сигнал «отойди», а не ошибка: тик не падает, цикл
    для этого монитора пропускается, событие попадает в журнал."""
    await _monitor(db, user, last_checked=None)
    flood = errors.FloodWaitError(request=None, capture=3600)

    await _worker(db, telegram=FakeTelegram(fail=flood))._default_tick()

    events = {log.event_type for log in await db.scalars(select(LogEntry))}
    assert "FLOOD_WAIT" in events, f"FloodWait не отмечен в журнале: {events}"


@pytest.mark.asyncio
async def test_avatar_is_stored_for_the_feed(db, user):
    """chat_avatars до сих пор только читалась эндпоинтом ленты — писать
    её некому, и аватарки не появлялись никогда (половина задачи 5.4)."""
    await _monitor(db, user, last_checked=None)
    await _worker(db)._default_tick()

    avatars = list(await db.scalars(select(ChatAvatar)))
    assert avatars, "аватарка канала не сохранена — лента останется без картинок"
    assert avatars[0].chat_id == -1001


# --------------------------------------------------------------------------
# Автоочистка
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_cleanup_runs_and_marks_the_run(db, user):
    old = LogEntry(
        user_id=user.id,
        event_type="OLD",
        details="старое",
        status="INFO",
        timestamp=_utc(days=90),
    )
    db.add(old)
    integration = Integration(
        user_id=user.id, cleanup_enabled=True, cleanup_days=30, cleanup_last_run=None
    )
    db.add(integration)
    await db.commit()

    await _worker(db)._default_tick()

    await db.refresh(integration)
    assert integration.cleanup_last_run is not None, "автоочистка не отметила запуск"
    remaining = [log.event_type for log in await db.scalars(select(LogEntry))]
    assert "OLD" not in remaining, "старая запись не удалена автоочисткой"


@pytest.mark.asyncio
async def test_cleanup_does_not_run_twice_a_day(db, user):
    """Раз в сутки, а не каждые 30 секунд: тик гоняется постоянно."""
    integration = Integration(
        user_id=user.id,
        cleanup_enabled=True,
        cleanup_days=30,
        cleanup_last_run=_utc(hours=1),
    )
    db.add(integration)
    await db.commit()
    # marker берётся ПОСЛЕ refresh, а не из посева: SQLite возвращает
    # datetime без tzinfo, и naive == aware — всегда False. Сравнение с
    # исходным значением падало бы независимо от поведения воркера.
    await db.refresh(integration)
    marker = integration.cleanup_last_run

    await _worker(db)._default_tick()

    await db.refresh(integration)
    assert integration.cleanup_last_run == marker, "очистка запустилась раньше суток"


@pytest.mark.asyncio
async def test_cleanup_is_disabled_by_default(db, user):
    integration = Integration(user_id=user.id, cleanup_enabled=False)
    db.add(integration)
    await db.commit()

    await _worker(db)._default_tick()

    await db.refresh(integration)
    assert integration.cleanup_last_run is None, "выключенная очистка отработала"
