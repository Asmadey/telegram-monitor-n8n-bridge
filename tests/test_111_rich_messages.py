"""Сводка с таблицей доезжает таблицей (задача 9.19).

Владелец получил в бота стену из вертикальных чёрточек: модель ответила
Markdown-таблицей на пятнадцать вакансий, а `sendMessage` таблиц не знает
вовсе — в его HTML нет ни `<table>`, ни заголовков, ни списков. Задача
9.17 привела разметку к тому, что понимает `sendMessage`, и это было
верно для своего момента; таблица в этот набор просто не помещалась.

**Bot API 10.1 добавил `sendRichMessage`** — проверено по документации, а
не по памяти: моё знание обрывается раньше, и я сперва усомнился в самом
существовании метода. Он меняет три вещи сразу:

| | `sendMessage` | `sendRichMessage` |
|---|---|---|
| длина | 4096 | 32768 |
| таблицы | нет | `<table bordered striped compact>` |
| заголовки и списки | нет | `<h1>`–`<h6>`, `<ul>`, `<ol>` |

`parse_mode` с ним НЕ передаётся: форматирование лежит внутри
`rich_message`, и старое поле в этом методе не участвует.

Запасной путь обязателен. Если метод почему-либо не отработал, сообщение
должно уйти прежним способом — но тогда таблица разворачивается в строки,
а не отправляется чёрточками: стена из `|` хуже отсутствия таблицы.
"""

import pytest

from app.services.telegram_markup import to_telegram_html

TABLE = """Вот сводка:

| ID | Вакансия | Стек |
|---|---|---|
| 1820 | ML-инженер (СберТех) | Python, SQL |
| 1819 | Вайб-кодер (BI.ZONE) | Claude Code, RAG |
"""


def test_markdown_table_becomes_a_real_table():
    out = to_telegram_html(TABLE)
    assert "<table" in out and "</table>" in out, (
        f"таблица осталась чёрточками: {out[:200]!r}"
    )
    assert "<th>ID</th>" in out, "шапка таблицы не распознана"
    assert "<td>1820</td>" in out
    assert "|---|" not in out, "разделитель Markdown уехал в сообщение как текст"


def test_table_cells_are_still_escaped():
    """Ячейку пишет модель по чужому посту — экранирование не отменяется."""
    out = to_telegram_html("| A | B |\n|---|---|\n| 5 < 10 | R&D |\n")
    assert "&lt;" in out and "&amp;" in out, f"ячейка не экранирована: {out!r}"


def test_text_around_the_table_survives():
    out = to_telegram_html(TABLE)
    assert "Вот сводка:" in out, "текст до таблицы потерян"


def test_headings_and_lists_use_real_tags():
    out = to_telegram_html("### Роли\n- Senior\n- Middle\n")
    assert "<h3>Роли</h3>" in out, f"заголовок остался жирным текстом: {out!r}"
    assert "<ul>" in out and "<li>Senior</li>" in out, f"список не собран: {out!r}"


def test_ordinary_text_is_unchanged():
    """Разметка без таблиц не должна поменяться от этой задачи."""
    out = to_telegram_html("📌 **Ключевая суть**: вакансии\n`#AI`")
    assert "<b>Ключевая суть</b>" in out and "<code>#AI</code>" in out


# --------------------------------------------------------------------------
# Доставка
# --------------------------------------------------------------------------


def test_plain_rendering_flattens_tables():
    """Запасной путь: таблица разворачивается в строки, а не в чёрточки."""
    from app.services.telegram_markup import to_plain_html

    out = to_plain_html(TABLE)
    assert "<table" not in out, "в sendMessage ушёл тег, которого он не знает"
    assert "|" not in out, f"стена из чёрточек осталась: {out!r}"
    assert "1820" in out and "ML-инженер" in out, "данные строки потеряны"


@pytest.mark.asyncio
async def test_delivery_prefers_rich_message():
    """Сводка уходит методом, который умеет таблицы."""
    import httpx

    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    from app.services import dispatch as dispatch_module

    transport = httpx.MockTransport(handler)
    await dispatch_module.send_telegram_bot_message(
        "123:ABC", "-100777", "<table><tr><th>A</th></tr></table>", transport=transport
    )

    assert "sendRichMessage" in seen["url"], f"ушло не тем методом: {seen['url']}"
    assert '"rich_message"' in seen["body"], "нет поля rich_message"
    assert "parse_mode" not in seen["body"], (
        "parse_mode передан вместе с rich_message — он там не участвует"
    )


@pytest.mark.asyncio
async def test_delivery_falls_back_to_plain_message():
    """Метод не отработал — сообщение всё равно уходит, но развёрнутым."""
    import httpx

    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "sendRichMessage" in url:
            return httpx.Response(400, json={"ok": False, "description": "no method"})
        return httpx.Response(200, json={"ok": True})

    from app.services import dispatch as dispatch_module

    transport = httpx.MockTransport(handler)
    sent = await dispatch_module.send_telegram_bot_message(
        "123:ABC", "-100777", to_telegram_html(TABLE), transport=transport
    )

    assert sent is True, "сообщение не ушло вовсе"
    assert any("sendMessage" in url for url in calls), (
        f"запасной путь не сработал: {calls}"
    )
