"""Перенос строки в богатом сообщении — тег, а не символ (задача 13.10).

Владелец получил в бота сводку из восьми вакансий **одной сплошной
строкой**: «Директор по развитию бизнеса Компания: bell integrator Формат:
Удалённо Поиск и привлечение…». В `feed_items.ai_analysis` при этом лежит
нормальный текст с 44 переводами строк, и `to_telegram_html` их сохраняет —
проверено на живой записи `#77`.

**Причина — в методе доставки, а не в промпте и не в модели.** С задачи 9.19
сводка уходит через `sendRichMessage`, а его поле `html` разбирается как
НАСТОЯЩИЙ HTML. Документация Bot API в разделе «Rich HTML style» перечисляет
строчные теги и заканчивает список строкой-примером:

    all the text above was on the same line

То есть перевод строки в исходнике — обычный пробельный символ, и абзацы
склеиваются. Перенос делают `<br>` и блочные теги (`<p>`, `<h1>`, `<ul>`,
`<blockquote>`), и в примерах документации многострочная цитата написана
именно через `<br>`.

Дефект тихий и старый: с 2026-09-09 каждая сводка приходила стеной. Таблицы
9.19 чинили ровно эту боль и починили — у таблицы структура блочная, — а
обычный абзац так и остался склеенным.

**Обратная сторона правки.** Запасной путь (`sendMessage` с
`parse_mode=HTML`) `<br>` не знает вовсе: в его списке тегов такого нет, а
неизвестный тег — это 400 и потеря оформления у всего сообщения. Поэтому
`rich_html_to_plain`, который уже разворачивает таблицы в строки, обязан
разворачивать и `<br>` обратно в `\n`. Одна правка без другой ломает
доставку, и тесты держат обе стороны.
"""

import re

import pytest

from app.services import dispatch as dispatch_module
from app.services.telegram_markup import (
    rich_html_to_plain,
    to_telegram_html,
)

# Живой случай: так выглядит находка в сводке «ТОП-вакансии».
FINDING = (
    '<a href="https://t.me/toplevel_job/22067"><b>Директор по развитию</b></a>\n'
    "<i>Компания:</i> bell integrator\n"
    "<i>Формат:</i> Удалённо\n"
    "Поиск и привлечение новых заказчиков."
)


def _visible(rich: str) -> str:
    """Текст, который увидит человек: `<br>` — перенос, теги — не текст."""
    text = re.sub(r"<br\s*/?>", "\n", rich)
    return re.sub(r"<[^<>]+>", "", text)


# --------------------------------------------------------------------------
# Богатое сообщение
# --------------------------------------------------------------------------


def test_a_line_inside_a_paragraph_survives_as_a_break():
    """Главный случай: строки находки не должны слипнуться в одну."""
    out = to_telegram_html(FINDING)
    assert "<br>" in out, f"перенос строки потерян, придёт стеной: {out!r}"
    assert _visible(out).count("\n") >= 3, (
        f"строк меньше, чем в исходнике: {_visible(out)!r}"
    )


def test_a_blank_line_stays_a_blank_line():
    """Пустая строка между вакансиями — единственное, что их разделяет."""
    out = to_telegram_html("<b>Первая</b>\n\n<b>Вторая</b>")
    assert "<br><br>" in out, f"абзацы склеились: {out!r}"


def test_a_code_block_keeps_real_newlines():
    """Внутри `<pre>` перенос делает сам пробел — `<br>` там лишний."""
    out = to_telegram_html("Пример:\n\n```\nfirst\nsecond\n```")
    block = re.search(r"<pre>(.*?)</pre>", out, re.S)
    assert block, f"блок кода потерян: {out!r}"
    assert "<br>" not in block.group(1), (
        f"в блок кода дописан тег переноса: {block.group(1)!r}"
    )
    assert "first\nsecond" in block.group(1), (
        f"строки кода склеились: {block.group(1)!r}"
    )


def test_structural_newlines_do_not_become_empty_lines():
    """Перевод строки между блочными тегами — это отступ исходника, не перенос.

    Иначе список из трёх пунктов приедет с пустой строкой после каждого.
    """
    out = to_telegram_html("- первый\n- второй\n- третий")
    assert "</li><li>" in out.replace("\n", ""), f"список разъехался: {out!r}"
    assert "<br>" not in out, f"между пунктами списка дописан перенос: {out!r}"


def test_nothing_is_lost_in_translation():
    """Антивакуум: вычистить переносы вместе с текстом — тоже «зелёный»."""
    out = to_telegram_html(FINDING)
    visible = _visible(out)
    for word in ("Директор по развитию", "bell integrator", "Удалённо", "заказчиков"):
        assert word in visible, f"текст потерян при расстановке переносов: {out!r}"


def test_running_it_twice_changes_nothing():
    """Сводка проходит через разбор не один раз — переносы не должны множиться."""
    once = to_telegram_html(FINDING)
    assert to_telegram_html(once) == once, "второй проход переписал разметку"


# --------------------------------------------------------------------------
# Доставка: богатый путь и запасной
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rich_request_carries_breaks():
    """То, что уходит в `sendRichMessage`, обязано нести переносы тегом."""
    import httpx

    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    await dispatch_module.send_telegram_bot_message(
        "123:ABC",
        "-100777",
        to_telegram_html(FINDING),
        transport=httpx.MockTransport(handler),
    )
    assert "<br>" in seen.get("body", ""), (
        f"в богатое сообщение ушёл текст без переносов: {seen.get('body', '')[:200]!r}"
    )


@pytest.mark.asyncio
async def test_the_plain_fallback_drops_the_break_tag():
    """`sendMessage` тега `<br>` не знает: неизвестный тег — 400 на всю сводку."""
    import httpx

    bodies: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "sendRichMessage" in str(request.url):
            return httpx.Response(400, json={"ok": False, "description": "no method"})
        bodies.append(body)
        return httpx.Response(200, json={"ok": True})

    await dispatch_module.send_telegram_bot_message(
        "123:ABC",
        "-100777",
        to_telegram_html(FINDING),
        transport=httpx.MockTransport(handler),
    )
    assert bodies, "запасной путь не сработал вовсе"
    assert "<br>" not in bodies[0], (
        f"на запасной путь ушёл тег, которого он не знает: {bodies[0][:200]!r}"
    )
    assert "\\n" in bodies[0], (
        f"на запасном пути переносы пропали совсем: {bodies[0][:200]!r}"
    )


def test_the_downgrade_is_a_function_not_a_side_effect():
    """Разворот `<br>` живёт там же, где разворот таблиц, — одним приёмом."""
    plain = rich_html_to_plain("первая<br>вторая<br/>третья")
    assert plain == "первая\nвторая\nтретья", f"разворот не сработал: {plain!r}"


def test_a_long_summary_is_still_cut_at_a_line_break():
    """Нарезка резала по концу строки — граница обязана переехать за переносом.

    `_chunks` отступает к концу строки, чтобы сводка не рвалась посреди
    предложения. Опора была на символ `\\n`; после 13.10 его в богатом
    сообщении почти нет, и без этой правки длинная сводка рубится где
    придётся — регрессия, которую видно только на восьми каналах.
    """
    line = "<b>Вакансия</b> достаточно длинное описание одной находки. "
    text = "<br>".join([line] * 900)
    parts = dispatch_module._chunks(text, dispatch_module.RICH_CHUNK)
    assert len(parts) > 1, "материала не хватило на две части — тест бессмыслен"
    for part in parts[:-1]:
        assert part.endswith("<br>"), f"кусок оборван посреди строки: {part[-60:]!r}"
    assert "".join(parts) == text, "нарезка потеряла или переписала текст"
