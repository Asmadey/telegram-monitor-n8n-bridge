"""Модель обернула ответ в ```-ограждение, и оно уехало в Telegram (находка владельца).

Из источника «VASILE LUNGO | INSIDER» пришло сообщение, начинающееся с
```` ```html ```` и заканчивающееся ```` ``` ````. Доказано на живой записи:
в `feed_items.ai_analysis` лежит ровно это, а внутри — правильные `<b>` и
`<a>`. Промпт обёртку не просил: так отвечают модели, когда их просят «оформи
в HTML».

`to_telegram_html` ограждения НЕ снимал и в `<pre>` не превращал — они
проходили насквозь и приезжали буквами. Причина в порядке разбора: текст
сначала режется по настоящим тегам, и разбор ограждений идёт уже внутри
кусков — открывающее попадает в один кусок, закрывающее в другой, пара не
находится никогда.

**Чинится не промптом.** Модуль об этом прямо говорит в первых строках:
«Промпт — не гарантия… Формат ответа — обязанность доставки». Просить модель
«не оборачивай» — та же ошибка, что просить её не ломать разметку.

Граница правки: снимается ОДНО ограждение, обнимающее весь ответ. Ограждение
внутри ответа — это пример кода, и он остаётся кодом.

Вторая половина находки — про доставку. У бота три пути: `sendRichMessage`,
затем `sendMessage` с `parse_mode=HTML`, затем `sendMessage` БЕЗ `parse_mode`.
Последний отправляет сообщение буквами — теги, ограждения и всё остальное — и
делает это молча. «Пришло без форматирования» сегодня нельзя узнать иначе как
глазами, и владелец так и узнал.
"""

import pytest
from sqlalchemy import select

from app.models import LogEntry
from app.services import dispatch as dispatch_module
from app.services.telegram_markup import to_telegram_html

FENCED = """```html
<b>📌 Тезисно</b>

• Первый тезис
<a href="https://t.me/c/1/2">🔗 Источник</a>
```"""


# --------------------------------------------------------------------------
# Ограждение вокруг всего ответа
# --------------------------------------------------------------------------


def test_a_fence_around_the_whole_answer_is_dropped():
    result = to_telegram_html(FENCED)
    assert "```" not in result, f"ограждение уехало в сообщение: {result[:120]!r}"
    assert "<b>📌 Тезисно</b>" in result, (
        f"разметка потеряна вместе с ограждением: {result!r}"
    )
    assert '<a href="https://t.me/c/1/2">' in result, "ссылка потеряна"


def test_the_language_tag_does_not_make_it_code():
    """```json и голое ``` — та же обёртка, что и ```html."""
    for opening in ("```json", "```", "```HTML"):
        answer = f"{opening}\n<b>текст</b>\n```"
        result = to_telegram_html(answer)
        assert "```" not in result, f"{opening}: ограждение осталось — {result!r}"


def test_an_unterminated_html_fence_is_dropped_too():
    """Модель начала ответ обёрткой и не закрыла её — это всё ещё обёртка."""
    result = to_telegram_html("```html\n<b>текст</b>")
    assert "```" not in result, f"незакрытая обёртка осталась: {result!r}"
    assert "<b>текст</b>" in result


# --------------------------------------------------------------------------
# Антивакуум: код остаётся кодом
# --------------------------------------------------------------------------


def test_a_code_sample_inside_the_answer_stays_code():
    answer = "Вот пример запроса:\n\n```\nGET /api/health\n```\n\nи это всё."
    result = to_telegram_html(answer)
    assert "<pre>" in result, f"пример кода перестал быть кодом: {result!r}"
    assert "GET /api/health" in result


def test_two_fences_are_never_unwrapped():
    """Два ограждения — это два примера кода, а не обёртка ответа."""
    answer = "```\nодин\n```\n\nмежду\n\n```\nдва\n```"
    result = to_telegram_html(answer)
    assert result.count("<pre>") == 2, f"ограждения съедены: {result!r}"


def test_an_unterminated_code_fence_is_left_alone():
    """```python — заявка на код, а не на обёртку ответа."""
    result = to_telegram_html("```python\nprint(1)")
    assert "```python" in result or "<pre>" in result, (
        f"незакрытый пример кода потерян: {result!r}"
    )


# --------------------------------------------------------------------------
# Молчащий путь доставки
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sender_reports_when_it_drops_the_markup():
    """Путь без `parse_mode` обязан сказать о себе — иначе о нём не узнать."""
    import httpx

    dropped: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "sendRichMessage" in str(request.url):
            return httpx.Response(400, json={"ok": False, "description": "no method"})
        if "parse_mode" in body:
            return httpx.Response(400, json={"ok": False, "description": "can't parse"})
        return httpx.Response(200, json={"ok": True})

    await dispatch_module.send_telegram_bot_message(
        "123:ABC",
        "-100777",
        "<b>текст</b>",
        transport=httpx.MockTransport(handler),
        on_degraded=lambda: dropped.append(True),
    )
    assert dropped, "сообщение ушло буквами, и об этом никто не узнал"


@pytest.mark.asyncio
async def test_a_normal_delivery_reports_nothing():
    """Антивакуум: обычная доставка не должна пугать записью в журнале."""
    import httpx

    dropped: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    await dispatch_module.send_telegram_bot_message(
        "123:ABC",
        "-100777",
        "<b>текст</b>",
        transport=httpx.MockTransport(handler),
        on_degraded=lambda: dropped.append(True),
    )
    assert not dropped, "богатый путь отработал, а отметка о потере разметки есть"


@pytest.mark.asyncio
async def test_the_journal_names_the_message_that_lost_its_markup(
    db, user, monkeypatch
):
    """Запись в журнале — единственный способ узнать о потере разметки."""
    from test_57_dispatch import _enable_all

    await _enable_all(db, user.id)

    async def fake_sender(token, chat_id, text, *, transport=None, on_degraded=None):
        if on_degraded:
            on_degraded()
        return True

    monkeypatch.setattr(dispatch_module, "send_telegram_bot_message", fake_sender)

    integration = await dispatch_module._integration(db, user.id)
    await dispatch_module._run_bot(
        db,
        user.id,
        integration,
        {"chat_title": "VASILE LUNGO | INSIDER"},
        [],
        "<b>текст</b>",
        None,
    )

    entries = list(
        await db.scalars(select(LogEntry).where(LogEntry.user_id == user.id))
    )
    details = " ".join((entry.details or "") for entry in entries)
    assert "разметк" in details.lower(), (
        f"журнал молчит о сообщении, ушедшем буквами: {details!r}"
    )


def test_an_answer_that_is_entirely_a_code_sample_stays_code():
    """Граница правила, и её задал `test_105`: язык решает.

    Пустое ограждение, `html`, `json`, `markdown` — так модель помечает
    ответ, который решила «показать» целиком. Имя настоящего языка — заявка
    на пример кода, и ответ из одного такого блока кодом и остаётся: иначе
    человек, просивший у модели кусок скрипта, получил бы его без
    моноширинного блока.
    """
    result = to_telegram_html("```python\nprint(1)\n```")
    assert "<pre>" in result and "print(1)" in result, result
