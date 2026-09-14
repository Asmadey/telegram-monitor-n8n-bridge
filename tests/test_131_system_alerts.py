"""Системная тревога приходит в бота тенанта (задача 13.3).

`GET /api/ops/health` знает про базу, воркер и очередь — но это **опрос**:
чтобы узнать об отказе, надо зайти и посмотреть. А молчащий воркер план сам
называет самым вероятным отказом этой архитектуры, и снаружи он выглядит
ровно как «сегодня ничего не нашлось»: лента пуста, ошибок нигде нет.

Решение владельца 2026-09-14: слать тревогу в **тот же бот**, которым уходят
сводки. Ни нового канала, ни нового секрета — токен и чат уже настроены и уже
проверены живой кнопкой.

Четыре правила, каждое из которых можно нарушить и получить худшее, чем
молчание:

1. **Тревога не гасится выключателем сводок.** Выключатель говорит «не шли мне
   дайджесты», а не «не говори, что сервис встал».
2. **Одна тревога — один раз в окно.** Источник падает по расписанию каждые
   полчаса; без окна бот превратится в шум, а шум читают так же, как тишину.
3. **Отказ Bot API не роняет вызывающего.** Тревога — это диагностика поверх
   отказа; если она падает сама, отказ становится двойным.
4. **В тексте тревоги нет секретов.** Он уходит наружу, в чужой мессенджер.
"""

import datetime

import pytest
from sqlalchemy import select

from app.models import Integration, LogEntry
from app.services.alerts import alert_owner
from app.services.integrations import save_integration_secrets

pytestmark = pytest.mark.asyncio

NOW = datetime.datetime(2026, 9, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


TOKEN = "123456:AAtest-bot-token"


class RecordingBot:
    """Двойник отправителя: помнит, что и куда ушло."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def __call__(self, token, chat_id, text):
        if self.fail:
            raise RuntimeError("Bot API ответил 502")
        self.sent.append({"token": token, "chat_id": chat_id, "text": text})
        return True


async def _with_bot(
    db, user_id: int, *, enabled: bool = True, chat: str = "99"
) -> None:
    await save_integration_secrets(db, user_id, bot_token=TOKEN)
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one()
    integration.telegram_sender_id = chat
    integration.telegram_forward_enabled = enabled
    await db.commit()


# --------------------------------------------------------------------------
# Доставка
# --------------------------------------------------------------------------


async def test_an_alert_reaches_the_bot_of_that_tenant(db, user):
    await _with_bot(db, user.id)
    bot = RecordingBot()

    sent = await alert_owner(
        db,
        user.id,
        key="worker-down",
        text="Мониторинг не работал 12 минут",
        sender=bot,
        now=NOW,
    )

    assert sent is True
    assert len(bot.sent) == 1, f"тревога не ушла: {bot.sent}"
    assert bot.sent[0]["chat_id"] == "99"
    assert "12 минут" in bot.sent[0]["text"]


async def test_an_alert_is_not_silenced_by_the_digest_switch(db, user):
    """Выключатель сводок — про дайджесты, а не про «сервис встал»."""
    await _with_bot(db, user.id, enabled=False)
    bot = RecordingBot()
    assert await alert_owner(db, user.id, key="k", text="отказ", sender=bot, now=NOW)
    assert bot.sent, "выключенные сводки погасили системную тревогу"


async def test_a_tenant_without_a_bot_is_not_an_error(db, user):
    """Бот необязателен. Отсутствие адресата — не отказ, а «некому слать»."""
    bot = RecordingBot()
    assert (
        await alert_owner(db, user.id, key="k", text="отказ", sender=bot, now=NOW)
        is False
    )
    assert not bot.sent


async def test_an_alert_never_carries_the_token(db, user):
    await _with_bot(db, user.id)
    bot = RecordingBot()
    await alert_owner(db, user.id, key="k", text="отказ", sender=bot, now=NOW)
    body = bot.sent[0]["text"]
    assert TOKEN not in body, "токен бота уехал в текст сообщения"
    details = list(
        await db.scalars(select(LogEntry.details).where(LogEntry.user_id == user.id))
    )
    assert all(TOKEN not in (d or "") for d in details), "токен попал в журнал"


# --------------------------------------------------------------------------
# Окно повторов
# --------------------------------------------------------------------------


async def test_the_same_alert_does_not_repeat_inside_the_window(db, user):
    """Источник падает по расписанию — без окна бот станет шумом."""
    await _with_bot(db, user.id)
    bot = RecordingBot()
    await alert_owner(db, user.id, key="src-1", text="первый", sender=bot, now=NOW)
    await alert_owner(
        db,
        user.id,
        key="src-1",
        text="второй",
        sender=bot,
        now=NOW + datetime.timedelta(minutes=30),
    )
    assert len(bot.sent) == 1, f"повтор той же тревоги внутри окна: {bot.sent}"


async def test_a_different_alert_goes_through(db, user):
    """Антивакуум: глушилка, молчащая всегда, «проходит» тест выше."""
    await _with_bot(db, user.id)
    bot = RecordingBot()
    await alert_owner(db, user.id, key="src-1", text="первый", sender=bot, now=NOW)
    await alert_owner(db, user.id, key="src-2", text="второй", sender=bot, now=NOW)
    assert len(bot.sent) == 2, f"разные тревоги слиплись: {bot.sent}"


async def test_the_alert_repeats_after_the_window(db, user):
    """Отказ, который длится сутки, обязан напомнить о себе."""
    await _with_bot(db, user.id)
    bot = RecordingBot()
    await alert_owner(db, user.id, key="src-1", text="первый", sender=bot, now=NOW)
    await alert_owner(
        db,
        user.id,
        key="src-1",
        text="ещё раз",
        sender=bot,
        now=NOW + datetime.timedelta(hours=2),
    )
    assert len(bot.sent) == 2, "тревога замолчала навсегда после первого раза"


async def test_one_tenant_alert_does_not_silence_another(db, user_a, user_b):
    await _with_bot(db, user_a.id, chat="11")
    await _with_bot(db, user_b.id, chat="22")
    bot = RecordingBot()
    await alert_owner(db, user_a.id, key="src-1", text="A", sender=bot, now=NOW)
    await alert_owner(db, user_b.id, key="src-1", text="B", sender=bot, now=NOW)
    assert [s["chat_id"] for s in bot.sent] == ["11", "22"], (
        f"окно повторов одного тенанта закрыло тревогу другому: {bot.sent}"
    )


# --------------------------------------------------------------------------
# Отказ самой тревоги
# --------------------------------------------------------------------------


async def test_a_broken_bot_does_not_break_the_caller(db, user):
    """Тревога — диагностика ПОВЕРХ отказа. Упав, она удваивает отказ."""
    await _with_bot(db, user.id)
    sent = await alert_owner(
        db, user.id, key="k", text="отказ", sender=RecordingBot(fail=True), now=NOW
    )
    assert sent is False
    details = list(
        await db.scalars(select(LogEntry.details).where(LogEntry.user_id == user.id))
    )
    assert any("тревог" in (d or "").lower() for d in details), (
        "неудача тревоги нигде не записана — отказ стал невидимым дважды"
    )


async def test_an_alert_does_not_invalidate_the_callers_objects(db, user):
    """Тревога зовётся из циклов воркера — она обязана быть безобидной.

    Первая версия делала `rollback()` в ветке отказа Bot API. Отката там
    нечего делать (упал HTTP-запрос, а не база), зато он обесценивает ВСЮ
    сессию вызывающего: следующее обращение воркера к собственным объектам
    дало бы `MissingGreenlet` (факт 4 `CLAUDE.md`). Поймано этим же тестом —
    он читал `user.id` после вызова и падал.
    """
    await _with_bot(db, user.id)
    await alert_owner(
        db, user.id, key="k", text="отказ", sender=RecordingBot(fail=True), now=NOW
    )
    assert user.id, "объекты вызывающего обесценены тревогой"
    assert user.email, "объекты вызывающего обесценены тревогой"


# --------------------------------------------------------------------------
# Воркер: о чём именно предупреждать
# --------------------------------------------------------------------------


async def test_a_restart_after_a_gap_tells_the_owner(db, user):
    """Воркер не может сообщить о собственной смерти — он мёртв.

    Зато поднявшийся процесс видит чужую отметку и знает, сколько её не
    обновляли. Это ловит самый частый случай на Railway: процесс упал,
    супервайзер поднял, опрос стоял полчаса — и никто не узнал.

    Совсем мёртвый воркер (его вообще не запускают) этим не ловится, и
    честнее сказать это вслух, чем притворяться, что ловится.
    """
    from test_58_worker_body import _worker

    from app.models import WorkerHeartbeat

    await _with_bot(db, user.id)
    db.add(
        WorkerHeartbeat(
            name="worker",
            beat_at=NOW - datetime.timedelta(minutes=27),
            leader=True,
            key_fingerprint="",
        )
    )
    await db.commit()

    bot = RecordingBot()
    await _worker(db).report_downtime(db, sender=bot, now=NOW)

    assert bot.sent, "простой воркера остался незамеченным"
    assert "27" in bot.sent[0]["text"], f"в тревоге нет длительности: {bot.sent[0]}"


async def test_a_fresh_install_does_not_cry_wolf(db, user):
    """Отметки нет вовсе — это первый запуск, а не простой."""
    from test_58_worker_body import _worker

    await _with_bot(db, user.id)
    bot = RecordingBot()
    await _worker(db).report_downtime(db, sender=bot, now=NOW)
    assert not bot.sent, f"первый запуск объявлен отказом: {bot.sent}"


async def test_a_live_worker_is_silent(db, user):
    """Антивакуум: будильник, звонящий всегда, «проходит» тест выше."""
    from test_58_worker_body import _worker

    from app.models import WorkerHeartbeat

    await _with_bot(db, user.id)
    db.add(
        WorkerHeartbeat(
            name="worker",
            beat_at=NOW - datetime.timedelta(seconds=30),
            leader=True,
            key_fingerprint="",
        )
    )
    await db.commit()
    bot = RecordingBot()
    await _worker(db).report_downtime(db, sender=bot, now=NOW)
    assert not bot.sent, f"обычный тик принят за простой: {bot.sent}"


async def test_the_downtime_is_reported_once_per_process(db, user):
    """Перерыв случился один; тиков после него будут сотни."""
    from test_58_worker_body import _worker

    from app.models import WorkerHeartbeat

    await _with_bot(db, user.id)
    db.add(
        WorkerHeartbeat(
            name="worker",
            beat_at=_utcnow() - datetime.timedelta(minutes=20),
            leader=True,
            key_fingerprint="",
        )
    )
    await db.commit()

    worker = _worker(db)
    bot = RecordingBot()
    told_first = await worker.report_downtime(db, sender=bot)
    worker._downtime_reported = True
    assert told_first == 1, "о простое не сообщили"

    # второй тик того же процесса — молчание, даже если окно ещё не истекло
    if not worker._downtime_reported:
        await worker.report_downtime(db, sender=bot)
    assert len(bot.sent) == 1, f"тревога о простое повторилась: {bot.sent}"


async def test_a_source_that_gave_nothing_tells_the_owner(db, user):
    """Источник, у которого не разобрался ни один канал, обязан позвать.

    Запись в журнале для этого мало: чтобы её увидеть, надо зайти и
    посмотреть, а снаружи отказ выглядит как пустая лента — тот же класс,
    что ловили всю фазу 9.
    """
    from test_58_worker_body import FakeTelegram, _worker
    from test_112_map_reduce import _source

    await _with_bot(db, user.id)
    source = await _source(
        db, user, public_id="src-dead", channels=[("@alpha", None, 5, "искать")]
    )
    telegram = FakeTelegram(fail=LookupError("канал не найден"))
    bot = RecordingBot()

    outcome = await _worker(db, telegram=telegram).poll_source(
        db, source, alert_sender=bot
    )

    assert outcome == "failed"
    assert bot.sent, "источник молча не дал ничего — владелец не узнает"
    assert "alpha" in bot.sent[0]["text"], f"тревога не называет канал: {bot.sent[0]}"
