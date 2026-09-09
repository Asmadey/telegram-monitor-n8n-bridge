"""Воркер никого не ждёт (задача 11.0, она же 9.16).

Найдено 9 сентября на проде: воркер перестал отмечаться, оставаясь живым.
`seconds_since_beat` рос — 213, 226, 249, — процесс не падал и не
перезапускался, а последней записью в его логе были ошибки разрешения
канала. Помог только перезапуск при выкладке.

Причина в том, что у внешних вызовов воркера нет тайм-аута НИ ОДНОГО.
`TelegramGateway.resolve`, `.fetch` и `.avatar` вызывают Telethon напрямую;
у HTTP-клиента OpenRouter тайм-аут есть (45 с), у MTProto — нет. Вызов,
который не возвращается, останавливает тик целиком, а с ним очередь,
расписание и очистку — то есть всех пользователей сразу.

Фаза 11 делает это критичным: сейчас в тике один внешний вызов на канал,
а станет шесть на источник (пять разборов и сведение). Поэтому тайм-ауты —
предусловие конвейера, а не улучшение.

**Отдельный вопрос — что считать признаком жизни.** Задача 10.2 ставила
удар сердца первым в тике осознанно: «процесс, зависший внутри тика,
обязан выглядеть мёртвым». Но прогон источника из пяти каналов законно
идёт минутами, и при отметке раз в тик такой прогон неотличим от
зависания.

Разрешается это тем, что удар сердца перестаёт быть отметкой ПРОЦЕССА и
становится отметкой ПРОДВИЖЕНИЯ: он ставится не только в начале тика, но и
после каждой законченной единицы работы. Долгий, но идущий прогон
отмечается между каналами и виден живым; зависший вызов не даёт дойти до
следующей отметки, и воркер по-прежнему выглядит мёртвым. Контракт 10.2 не
ослаблен — он уточнён.
"""

import asyncio
import datetime

import pytest
from sqlalchemy import select
from test_58_worker_body import FakeEntity, FakeTelegram, _monitor, _utc, _worker

from app.models import LogEntry, WorkerHeartbeat


class HangingTelegram(FakeTelegram):
    """Шлюз, который не возвращается. Ровно то, что случилось на проде."""

    def __init__(self, *, hang_on="fetch", **kwargs):
        super().__init__(**kwargs)
        self.hang_on = hang_on
        self.entered = asyncio.Event()

    async def resolve(self, client, target):
        if self.hang_on == "resolve":
            self.entered.set()
            await asyncio.sleep(3600)
        return FakeEntity()

    async def fetch(self, client, entity, *, limit, offset_hours):
        if self.hang_on == "fetch":
            self.entered.set()
            await asyncio.sleep(3600)
        return list(self.posts)


async def _poll_errors(db) -> list[LogEntry]:
    return list(
        await db.scalars(select(LogEntry).where(LogEntry.event_type == "POLL_ERROR"))
    )


@pytest.mark.parametrize("hang_on", ["resolve", "fetch"])
@pytest.mark.asyncio
async def test_hanging_call_is_cut_and_the_tick_goes_on(db, user, hang_on, monkeypatch):
    """Вызов, который не возвращается, обязан быть прерван."""
    from app import worker as worker_module

    monkeypatch.setattr(worker_module, "TELEGRAM_TIMEOUT", 0.2)
    await _monitor(db, user, last_checked=_utc(hours=3))
    worker = _worker(db, telegram=HangingTelegram(hang_on=hang_on))

    await asyncio.wait_for(worker.run_schedule(db), timeout=10)

    rows = await _poll_errors(db)
    assert rows, "зависший вызов не оставил следа в журнале"
    assert "TimeoutError" in rows[0].details or "тайм-аут" in rows[0].details.lower(), (
        f"причина не названа тайм-аутом: {rows[0].details!r}"
    )


@pytest.mark.asyncio
async def test_timeout_is_not_reported_as_success(db, user, monkeypatch):
    """Прерванный опрос — не «опрошено». Иначе тишина выглядит нормой."""
    from app import worker as worker_module

    monkeypatch.setattr(worker_module, "TELEGRAM_TIMEOUT", 0.2)
    await _monitor(db, user, last_checked=_utc(hours=3))
    worker = _worker(db, telegram=HangingTelegram())

    polled = await asyncio.wait_for(worker.run_schedule(db), timeout=10)

    assert polled == 0, "прерванный по тайм-ауту опрос посчитан успешным"


@pytest.mark.asyncio
async def test_one_hung_channel_does_not_block_the_others(db, user, monkeypatch):
    """Сломанный канал не забирает остальных с собой."""
    from app import worker as worker_module

    monkeypatch.setattr(worker_module, "TELEGRAM_TIMEOUT", 0.2)

    class OneHangs(FakeTelegram):
        async def fetch(self, client, entity, *, limit, offset_hours):
            self.fetches.append(getattr(entity, "username", "?"))
            if len(self.fetches) == 1:
                await asyncio.sleep(3600)
            return list(self.posts)

    await _monitor(db, user, public_id="mon-1", last_checked=_utc(hours=3))
    await _monitor(
        db, user, public_id="mon-2", chat_id=-1002, last_checked=_utc(hours=3)
    )
    telegram = OneHangs()
    worker = _worker(db, telegram=telegram)

    await asyncio.wait_for(worker.run_schedule(db), timeout=10)

    assert len(telegram.fetches) == 2, (
        "после зависшего канала второй даже не пробовали опросить"
    )


@pytest.mark.asyncio
async def test_heartbeat_marks_progress_not_just_the_start_of_the_tick(
    db, user, monkeypatch
):
    """Долгий, но идущий прогон обязан выглядеть живым.

    При отметке раз в тик прогон источника из пяти каналов (законно
    минуты) неотличим от зависания: порог устаревания — 180 секунд.
    """
    from app.services.ops import record_heartbeat

    await _monitor(db, user, public_id="mon-1", last_checked=_utc(hours=3))
    await _monitor(
        db, user, public_id="mon-2", chat_id=-1002, last_checked=_utc(hours=3)
    )
    worker = _worker(db, telegram=FakeTelegram())

    # состарим отметку так, чтобы её обновление было заметно
    await record_heartbeat(db, "worker", leader=True)
    beat = (await db.scalars(select(WorkerHeartbeat))).first()
    beat.beat_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=600
    )
    await db.commit()

    await worker.run_schedule(db)

    beat = (await db.scalars(select(WorkerHeartbeat))).first()
    age = (
        datetime.datetime.now(datetime.timezone.utc) - beat.beat_at.replace(tzinfo=None)
        if beat.beat_at.tzinfo is None
        else datetime.datetime.now(datetime.timezone.utc) - beat.beat_at
    ).total_seconds()
    assert age < 60, (
        "расписание отработало, а отметка не обновилась: долгий прогон "
        "будет неотличим от зависшего"
    )


@pytest.mark.asyncio
async def test_hung_call_still_stops_the_heartbeat(db, user, monkeypatch):
    """Контракт 10.2 не ослаблен: зависший воркер выглядит мёртвым.

    Отметка ставится МЕЖДУ единицами работы, а не по таймеру в отдельной
    задаче. Поэтому вызов, который не возвращается, до следующей отметки
    дойти не даёт — и снаружи это по-прежнему «воркер мёртв».
    """
    import inspect

    from app import worker as worker_module

    source = inspect.getsource(worker_module)
    assert "asyncio.create_task" not in source or "heartbeat" not in source.lower(), (
        "удар сердца вынесен в независимую задачу — тогда зависший тик "
        "снова выглядит живым, а это ровно то, что 10.2 запрещает"
    )


@pytest.mark.asyncio
async def test_tick_budget_stops_taking_new_work(db, user, monkeypatch):
    """Бюджет тика: исчерпан — новые единицы работы не начинаются.

    Без потолка тик растягивается на столько, сколько дают внешние
    сервисы, и очередь с расписанием стоят всё это время.
    """
    from app import worker as worker_module

    monkeypatch.setattr(worker_module, "TELEGRAM_TIMEOUT", 0.2)
    monkeypatch.setattr(worker_module, "TICK_BUDGET", 0.3)

    class Slow(FakeTelegram):
        async def fetch(self, client, entity, *, limit, offset_hours):
            self.fetches.append(getattr(entity, "username", "?"))
            await asyncio.sleep(0.15)
            return list(self.posts)

    for index in range(5):
        await _monitor(
            db,
            user,
            public_id=f"mon-{index}",
            chat_id=-1000 - index,
            last_checked=_utc(hours=3),
        )
    telegram = Slow()
    worker = _worker(db, telegram=telegram)

    await asyncio.wait_for(worker.run_schedule(db), timeout=10)

    assert len(telegram.fetches) < 5, (
        "бюджет тика не соблюдается: воркер разбирает всё подряд, сколько бы "
        "времени это ни заняло"
    )


@pytest.mark.asyncio
async def test_our_timeout_is_not_confused_with_the_calls_own():
    """Чужой тайм-аут не выдаётся за наш.

    `asyncio.wait_for` отдаёт `TimeoutError` и когда истекло НАШЕ время, и
    когда вызов упал по своему тайм-ауту. Первая версия обёртки ловила оба
    и подписывала «тайм-аут 60 с» — то есть врала о причине и о времени.
    Поймано тестом 9.11, который подаёт `asyncio.TimeoutError` как отказ
    сети; здесь это закреплено отдельно.
    """
    from app.worker import StepTimeout, _within

    async def fails_fast():
        raise TimeoutError("сеть не ответила")

    with pytest.raises(TimeoutError) as failure:
        await _within(fails_fast(), 60.0, "какой-то вызов")
    assert not isinstance(failure.value, StepTimeout), (
        "отказ самого вызова подписан нашим тайм-аутом — причина и время "
        f"в записи будут неверны: {failure.value!r}"
    )

    async def never_returns():
        await asyncio.sleep(3600)

    with pytest.raises(StepTimeout) as ours:
        await _within(never_returns(), 0.05, "зависший вызов")
    assert "тайм-аут" in str(ours.value)
