"""Диспетчеризация: AI → Telegram-бот → n8n → лента (порт server.py:882).

Монолитный `process_and_dispatch_messages` собирал всю доставку в одной
функции на глобальных настройках. Порт делает то же самое в разрезе
тенанта — и чинит один дефект оригинала, который стоит проверять явно.

**Дефект оригинала.** Шаг 3 (вебхук) при успехе делает `return` (server.py:965)
— и шаг 4 (запись в ленту) не выполняется никогда. То есть лента
наполнялась ТОЛЬКО когда вебхук выключен или упал: у пользователя с
работающим n8n история в интерфейсе оставалась пустой, и это выглядело как
«лента не работает», а не как «доставка отработала». Порт пишет ленту
всегда — она история выполнения, а не запасной путь доставки.

Все внешние вызовы инъектируются: тесты не ходят ни в OpenRouter, ни в
Telegram, ни в n8n (autouse-страж conftest это и не позволил бы).
Исключение — тест SSRF: там вызывается НАСТОЯЩИЙ send_webhook, и проверка
адреса обязана отбить запрос ДО обращения к сети.
"""

import json

import pytest
from sqlalchemy import select

from app.models import FeedItem, LogEntry
from app.services.integrations import (
    save_integration_secrets,
    update_integration_config,
)

PAYLOAD = {
    "chat_id": -1001,
    "chat_title": "Канал",
    "chat_username": "channel",
    "messages": [
        {"id": 11, "text": "первый пост", "post_url": "https://t.me/channel/11"},
        {"id": 12, "text": "второй пост", "post_url": "https://t.me/channel/12"},
    ],
}


class Recorder:
    """Двойник исходящего вызова: запоминает аргументы, ничего не шлёт."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.calls: list[tuple] = []
        self._result = result
        self._raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


async def _enable_all(db, user_id, *, webhook_url="https://n8n.example.com/hook"):
    await save_integration_secrets(
        db,
        user_id,
        bot_token="123456789:AAbbccddeeffgghhiijjkkllmmnnooppqqr",
        openrouter_api_key="or-test-key",
        webhook_url=webhook_url,
    )
    await update_integration_config(
        db,
        user_id,
        {
            "openrouter_enabled": True,
            "telegram_forward_enabled": True,
            "telegram_sender_id": "-100777",
            "auto_webhook_enabled": True,
        },
    )


async def _logs(db, user_id) -> list[LogEntry]:
    return list(await db.scalars(select(LogEntry).where(LogEntry.user_id == user_id)))


async def _feed(db, user_id) -> list[FeedItem]:
    return list(await db.scalars(select(FeedItem).where(FeedItem.user_id == user_id)))


@pytest.mark.asyncio
async def test_full_chain_runs_in_order(db, user):
    """Включены все три интеграции — отрабатывают все три и лента."""
    from app.services.dispatch import dispatch

    llm = Recorder(result=("анализ батча", 120))
    bot = Recorder(result=True)
    hook = Recorder(result=200)

    await _enable_all(db, user.id)
    await dispatch(
        db,
        user.id,
        dict(PAYLOAD),
        llm_caller=llm,
        bot_sender=bot,
        webhook_sender=hook,
    )

    assert llm.calls, "LLM не вызван при включённом OpenRouter"
    assert bot.calls, "бот не вызван при включённой пересылке"
    assert hook.calls, "вебхук не вызван при включённой автоотправке"

    items = await _feed(db, user.id)
    assert len(items) == 1, "лента не пополнилась"
    assert items[0].ai_analysis == "анализ батча"
    assert items[0].messages_count == 2


@pytest.mark.asyncio
async def test_feed_is_written_even_when_webhook_succeeded(db, user):
    """Дефект оригинала (server.py:965): успешный вебхук делал `return`
    ДО записи в ленту, и история оставалась пустой у всех, у кого n8n
    работает. Лента — история выполнения, а не запасной путь."""
    from app.services.dispatch import dispatch

    await _enable_all(db, user.id)
    hook = Recorder(result=200)
    await dispatch(
        db,
        user.id,
        dict(PAYLOAD),
        llm_caller=Recorder(result=("a", 1)),
        bot_sender=Recorder(result=True),
        webhook_sender=hook,
    )

    assert hook.calls, "вебхук не отправлен — тест проверяет не то"
    assert await _feed(db, user.id), (
        "успешная отправка вебхука отменила запись в ленту — "
        "ровно дефект монолита, ради которого написан этот тест"
    )


@pytest.mark.asyncio
async def test_webhook_failure_does_not_swallow_the_feed(db, user):
    """Упавший n8n не должен стирать историю: лента пишется, ошибка — в журнал."""
    from app.services.dispatch import dispatch

    await _enable_all(db, user.id)
    await dispatch(
        db,
        user.id,
        dict(PAYLOAD),
        llm_caller=Recorder(result=("a", 1)),
        bot_sender=Recorder(result=True),
        webhook_sender=Recorder(raises=RuntimeError("n8n лежит")),
    )

    assert await _feed(db, user.id), "падение вебхука отменило запись в ленту"
    events = {log.event_type for log in await _logs(db, user.id)}
    assert "WEBHOOK_ERROR" in events, f"ошибка вебхука не в журнале: {events}"


@pytest.mark.asyncio
async def test_disabled_integrations_send_nothing(db, user):
    """Выключенные интеграции не трогаются вовсе, но лента ведётся:
    пользователь без n8n и бота всё равно видит, что канал опрошен."""
    from app.services.dispatch import dispatch

    bot, hook = Recorder(result=True), Recorder(result=200)
    await dispatch(db, user.id, dict(PAYLOAD), bot_sender=bot, webhook_sender=hook)

    assert not bot.calls, "бот вызван при выключенной пересылке"
    assert not hook.calls, "вебхук вызван при выключенной автоотправке"
    assert await _feed(db, user.id), "лента не ведётся без интеграций"


@pytest.mark.asyncio
async def test_private_webhook_url_is_never_requested(db, user):
    """SSRF-контур на пути ДОСТАВКИ, а не только сохранения.

    Здесь НЕ инъектируется отправитель: вызывается настоящий send_webhook,
    и адрес во внутренней сети обязан быть отбит до обращения к сети.
    Проверка на сохранении без проверки на отправке закрывает только
    парадную дверь: адрес мог быть сохранён до появления валидации или
    перенесён скриптом миграции.
    """
    from app.services.dispatch import dispatch

    await _enable_all(
        db, user.id, webhook_url="http://169.254.169.254/latest/meta-data/"
    )
    await dispatch(db, user.id, dict(PAYLOAD), bot_sender=Recorder(result=True))

    events = {log.event_type for log in await _logs(db, user.id)}
    assert "WEBHOOK_ERROR" in events, (
        "адрес метаданных облака не отбит и не попал в журнал"
    )
    assert await _feed(db, user.id), "лента не записана при отбитом вебхуке"


@pytest.mark.asyncio
async def test_bot_is_not_called_without_token_or_chat(db, user):
    """Включённый флаг без токена или чата — не повод дёргать Bot API
    (порт гейта server.py:856: пустой токен/чат → False без запроса)."""
    from app.services.dispatch import dispatch

    await update_integration_config(
        db, user.id, {"telegram_forward_enabled": True, "telegram_sender_id": ""}
    )
    bot = Recorder(result=True)
    await dispatch(db, user.id, dict(PAYLOAD), bot_sender=bot)
    assert not bot.calls, "бот вызван без адреса получателя"


@pytest.mark.asyncio
async def test_feed_item_belongs_to_the_dispatching_tenant(db, user_a, user_b):
    """Лента пишется тенанту задачи. В воркере user_id приходит ИЗ СТРОКИ
    задачи, а не из сессии пользователя — единственное место в проекте с
    таким источником, и ошибка здесь означает чужие посты в чужой ленте."""
    from app.services.dispatch import dispatch

    await dispatch(db, user_b.id, dict(PAYLOAD))

    assert not await _feed(db, user_a.id), "лента записана не тому пользователю"
    items = await _feed(db, user_b.id)
    assert len(items) == 1 and items[0].user_id == user_b.id


@pytest.mark.asyncio
async def test_raw_messages_are_stored_for_reanalysis(db, user):
    """Кнопка «переанализировать» берёт исходные посты из raw_messages_json
    (app/api/feed.py:150): без них задача гарантированно упадёт в воркере."""
    from app.services.dispatch import dispatch

    await dispatch(db, user.id, dict(PAYLOAD))
    item = (await _feed(db, user.id))[0]
    stored = json.loads(item.raw_messages_json or "[]")
    assert [m["id"] for m in stored] == [11, 12]
