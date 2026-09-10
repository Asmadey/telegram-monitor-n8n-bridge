"""Конвейер источника: разбор по каналам → сведение (задача 11.4).

Схема владельца: у источника пять каналов, у каждого свой лимит сообщений
и свой промпт извлечения. Сначала пять отдельных разборов — по одному на
канал, — затем шестой прогон сводит их вердикты в один ответ.

По токенам это почти не дороже одного большого запроса: посты уходят в
модель один раз, добавляется запрос на коротких выжимках. Взамен —
маленький контекст на каждом разборе (модель не «теряет середину» на
длинных каналах), изоляция сбоя одного канала и единственное место, где
можно выкинуть одну вакансию, висящую в трёх каналах сразу.

**Главный принцип: «совпадений нет» и «не смогли посмотреть» — разные
ответы.** Смешать их значит показать тишину там, где канал не
открывался. Поэтому у канала не вердикт, а исход, и неразобранные каналы
названы в самом сообщении — строкой, которую пишет система, а не модель:
модель может её проигнорировать или переврать.
"""

import json

import pytest
from sqlalchemy import select
from test_58_worker_body import FakeEntity, FakeTelegram, _utc, _worker

from app.models import FeedItem, LogEntry, Monitor, MonitorChannel

pytestmark = pytest.mark.asyncio


def _post(msg_id: int, text: str = "пост") -> dict:
    return {"id": msg_id, "text": text, "post_url": f"https://t.me/c/{msg_id}"}


async def _source(db, user, *, channels, **overrides) -> Monitor:
    fields = {
        "user_id": user.id,
        "public_id": overrides.pop("public_id", "src-1"),
        "title": "Вакансии продаж",
        "answer_prompt": "свести и оформить",
        "interval_minutes": 60,
        "is_active": True,
        "last_run_at": _utc(hours=3),
    }
    fields.update(overrides)
    source = Monitor(**fields)
    db.add(source)
    await db.commit()
    for position, (target, chat_id, limit, prompt) in enumerate(channels):
        db.add(
            MonitorChannel(
                monitor_id=source.id,
                user_id=user.id,
                chat_target=target,
                chat_title=target.lstrip("@"),
                chat_id=chat_id,
                limit_count=limit,
                extract_prompt=prompt,
                position=position,
            )
        )
    await db.commit()
    return source


class ChannelTelegram(FakeTelegram):
    """Каждый канал отдаёт свои посты; лимит запоминается."""

    def __init__(self, posts_by_chat: dict, fail_on: str | None = None):
        super().__init__()
        self.posts_by_chat = posts_by_chat
        self.fail_on = fail_on
        self.limits: dict[str, int] = {}

    async def resolve(self, client, target):
        """Разрешает и по chat_id, и по ссылке — как настоящий шлюз.

        Двойник обязан принимать оба: воркер пробует сначала `chat_id`, и
        двойник, знающий только строки, падал бы там, где живой шлюз
        отвечает (дефект двойника, пойманный красной фазой).
        """
        by_id = {cid: name for cid, (name, _) in self.posts_by_chat.items()}
        name = by_id.get(target) if isinstance(target, int) else target
        if name is None or self.fail_on == name:
            raise LookupError(f"канал {target} не найден")
        chat_id = next(
            (cid for cid, (title, _) in self.posts_by_chat.items() if title == name),
            None,
        )
        if chat_id is None:
            raise LookupError(f"канал {target} не найден")
        return FakeEntity(
            chat_id=chat_id, title=name.lstrip("@"), username=name.lstrip("@")
        )

    async def fetch(self, client, entity, *, limit, offset_hours):
        self.limits[f"@{entity.username}"] = limit
        return list(self.posts_by_chat[entity.id][1])


class RecordingLLM:
    """Двойник модели: помнит каждый вызов и отвечает по сценарию."""

    def __init__(self, answers=None, default="находка"):
        self.calls: list[dict] = []
        self.answers = answers or {}
        self.default = default

    async def __call__(self, db, user_id, messages, **kwargs):
        prompt = kwargs.get("custom_prompt") or ""
        self.calls.append({"prompt": prompt, "messages": list(messages)})
        for needle, answer in self.answers.items():
            if needle in prompt:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return self.default


async def _run(db, source, worker, **senders):
    """Опрос источника. Разбор идёт ТУТ ЖЕ, поэтому двойники доставки
    передаются сюда, а не в `run_jobs`: батч выполняется на месте, чтобы
    сбой доставки не заставлял заново читать Telegram."""
    return await worker.poll_source(db, source, **senders)


THREE = {
    -1001: ("@alpha", [_post(11), _post(12)]),
    -1002: ("@beta", [_post(21)]),
    -1003: ("@gamma", [_post(31)]),
}


async def test_each_channel_is_analysed_with_its_own_prompt_and_limit(db, user):
    source = await _source(
        db,
        user,
        channels=[
            ("@alpha", -1001, 50, "извлекать продажи"),
            ("@beta", -1002, 5, "извлекать маркетинг"),
            ("@gamma", -1003, 20, "извлекать аналитику"),
        ],
    )
    telegram = ChannelTelegram(THREE)
    llm = RecordingLLM()
    worker = _worker(db, telegram=telegram)
    worker.llm = llm

    await _run(db, source, worker)

    prompts = [c["prompt"] for c in llm.calls]
    assert "извлекать продажи" in prompts, (
        f"канал разобран не своим промптом: {prompts}"
    )
    assert "извлекать маркетинг" in prompts
    assert "извлекать аналитику" in prompts
    assert "свести и оформить" in prompts, "сведения не было вовсе"
    assert len(llm.calls) == 4, (
        f"вызовов {len(llm.calls)}, ожидалось 3 разбора + сведение"
    )
    assert telegram.limits == {"@alpha": 50, "@beta": 5, "@gamma": 20}, (
        f"лимит канала не доехал до выборки: {telegram.limits}"
    )


async def test_reduce_sees_verdicts_in_channel_order(db, user):
    """Порядок каналов задаёт порядок в сводке."""
    source = await _source(
        db,
        user,
        channels=[
            ("@alpha", -1001, 20, "первый"),
            ("@beta", -1002, 20, "второй"),
        ],
    )
    llm = RecordingLLM(answers={"первый": "вердикт А", "второй": "вердикт Б"})
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    reduce_call = llm.calls[-1]
    joined = json.dumps(reduce_call["messages"], ensure_ascii=False)
    assert joined.index("вердикт А") < joined.index("вердикт Б"), (
        f"сведение получило каналы не в порядке источника: {joined[:300]}"
    )


async def test_one_broken_channel_does_not_cancel_the_others(db, user):
    source = await _source(
        db,
        user,
        channels=[
            ("@alpha", -1001, 20, "первый"),
            ("@beta", -1002, 20, "второй"),
        ],
    )
    telegram = ChannelTelegram(THREE, fail_on="@alpha")
    llm = RecordingLLM()
    worker = _worker(db, telegram=telegram)
    worker.llm = llm

    await _run(db, source, worker)

    assert any("второй" in c["prompt"] for c in llm.calls), (
        "сломанный канал забрал с собой исправный"
    )
    errors = [
        e.details
        for e in await db.scalars(select(LogEntry).where(LogEntry.status == "ERROR"))
    ]
    assert any("LookupError" in (d or "") for d in errors), (
        f"причина отказа канала не названа: {errors}"
    )


async def test_unparsed_channels_are_named_in_the_message(db, user):
    """Строку про неразобранные пишет система, а не модель."""
    source = await _source(
        db,
        user,
        channels=[
            ("@alpha", -1001, 20, "первый"),
            ("@beta", -1002, 20, "второй"),
        ],
    )
    from test_57_dispatch import Recorder, _enable_all

    await _enable_all(db, user.id)
    bot = Recorder(result=True)
    # настоящая доставка: помощник тестов по умолчанию подменяет её
    # двойником, а здесь проверяется именно текст ушедшего сообщения
    from app.services.dispatch import dispatch as real_dispatch

    worker = _worker(
        db,
        telegram=ChannelTelegram(THREE, fail_on="@alpha"),
        dispatcher=real_dispatch,
    )
    worker.llm = RecordingLLM()

    await _run(db, source, worker, bot_sender=bot)

    assert bot.calls, "сообщение не ушло"
    sent = bot.calls[0][0][2]
    assert "Не разобраны" in sent and "alpha" in sent, (
        f"неразобранный канал не назван: {sent!r}"
    )


async def test_no_findings_means_silence(db, user):
    """Совпадений нет — бот молчит, карточка в ленте всё равно есть."""
    from test_57_dispatch import Recorder, _enable_all

    source = await _source(db, user, channels=[("@alpha", -1001, 20, "первый")])
    await _enable_all(db, user.id)
    bot = Recorder(result=True)
    from app.services.dispatch import dispatch as real_dispatch

    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=real_dispatch)
    worker.llm = RecordingLLM(default="")

    await _run(db, source, worker, bot_sender=bot)

    assert bot.calls == [], "бот заговорил, хотя находок нет"
    assert list(await db.scalars(select(FeedItem))), "карточка в ленту не записана"


async def test_empty_answer_is_logged_not_swallowed(db, user):
    """Пустой ответ при непустом входе — самый вероятный тихий отказ."""
    source = await _source(db, user, channels=[("@alpha", -1001, 20, "первый")])
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = RecordingLLM(default="")

    await _run(db, source, worker)

    events = [e.event_type for e in await db.scalars(select(LogEntry))]
    assert "AI_EMPTY" in events, f"пустой ответ модели нигде не отмечен: {events}"


async def test_single_channel_costs_one_call(db, user):
    """Частый случай не должен стоить вдвое: сведение не нужно."""
    source = await _source(db, user, channels=[("@alpha", -1001, 20, "первый")])
    llm = RecordingLLM()
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    assert len(llm.calls) == 1, (
        f"на один канал ушло {len(llm.calls)} запросов — сведению сводить нечего"
    )


async def test_source_without_channels_is_not_polled(db, user):
    source = await _source(db, user, channels=[])
    worker = _worker(db, telegram=ChannelTelegram(THREE))

    result = await _run(db, source, worker)

    assert result == "no_channels", f"источник без каналов опрошен: {result}"


async def test_schedule_uses_the_source_clock(db, user):
    """`_is_due` считает по `last_run_at`: `last_checked` уехал в канал."""
    await _source(
        db,
        user,
        channels=[("@alpha", -1001, 20, "первый")],
        last_run_at=_utc(minutes=5),
    )
    worker = _worker(db, telegram=ChannelTelegram(THREE))

    polled = await worker.run_schedule(db)

    assert polled == 0, "источник опрошен раньше своего интервала"
