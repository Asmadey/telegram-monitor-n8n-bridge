"""Теги внутри кода Markdown рвали разметку всего сообщения (задача 13.25).

Найдено 2026-10-15 на источнике «БАЛИ»: в журнале `TG_BOT_PLAIN`, сообщение
ушло буквами. Ответ модели объяснял, как писать HTML для Telegram, и был полон
примеров: блоки ```html … ``` с `<b>`, `<pre>`, `<a>` внутри и теги в обратных
кавычках прямо в тексте.

**Причина — разбор, а не модель.** Теги искались по всему ответу, в том числе
внутри кода Markdown. `<pre>` из примера становился настоящим тегом, ограждение
``` рвалось по его границе и уже не находило пары. На тексте той сводки после
разбора оставалось около тридцати незакрытых тегов и десять ограждений буквами
— такую разметку Bot API не примет ни одним методом.

Код в Markdown — буквальный текст: что внутри ограждения или обратных кавычек,
показывается как написано. Текст ниже — синтетический, той же формы, что
сводка: заголовок, ограждения с HTML, теги в обратных кавычках, таблица.
"""

import re

from app.services.telegram_markup import rich_html_to_plain, to_telegram_html

ANSWER = """# Вердикт: так отправить нельзя

Telegram отклонит сообщение с ошибкой:

```
400 Bad Request: can't parse entities: Unsupported start tag "html"
```

При `parse_mode=HTML` поддерживаются `<b>`, `<i>`, `<pre>`, `<code>` и `<blockquote>`.

```html
<b>Сводка</b>
<pre>
37090  Если в воскресенье, то я +
</pre>
<a href="https://t.me/c/1/2" target="_blank">2</a>
```

| Проблема | Где |
|---|---|
| Документ: `<!DOCTYPE>`, `<html>` | начало |
| Перенос `<br>` | везде |

Итог: **переписать** фрагментом.
"""

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^<>]*>")
_VOID = {"br", "hr"}


def _unbalanced(markup: str) -> list[str]:
    """Нарушения вложенности: Bot API отвергает сообщение целиком за любое."""
    stack: list[str] = []
    found: list[str] = []
    for match in _TAG.finditer(markup):
        closing, name = match.group(1), match.group(2).lower()
        if name in _VOID:
            continue
        if not closing:
            stack.append(name)
        elif stack and stack[-1] == name:
            stack.pop()
        else:
            found.append(f"лишний </{name}> при открытых {stack}")
    if stack:
        found.append(f"не закрыты: {stack}")
    return found


def test_tags_inside_markdown_code_do_not_break_nesting():
    out = to_telegram_html(ANSWER)
    assert _unbalanced(out) == [], f"разметка разорвана: {_unbalanced(out)}"


def test_the_fallback_markup_is_balanced_too():
    """Запасной путь `sendMessage` получает развёрнутую разметку — она тоже цела."""
    plain = rich_html_to_plain(to_telegram_html(ANSWER))
    assert _unbalanced(plain) == [], (
        f"запасная разметка разорвана: {_unbalanced(plain)}"
    )


def test_no_fence_reaches_telegram_as_letters():
    out = to_telegram_html(ANSWER)
    assert "```" not in out, f"ограждение доехало буквами: {out!r}"


def test_a_tag_in_a_code_block_is_shown_as_written():
    out = to_telegram_html(ANSWER)
    assert "&lt;pre&gt;" in out and "&lt;b&gt;Сводка&lt;/b&gt;" in out, (
        f"пример кода исчез или стал разметкой: {out!r}"
    )
    assert 'target="_blank"' not in out or "&lt;a href=" in out, (
        f"ссылка из примера кода стала настоящей ссылкой: {out!r}"
    )


def test_a_tag_in_backticks_is_shown_as_written():
    out = to_telegram_html(ANSWER)
    assert "<code>&lt;pre&gt;</code>" in out, f"тег в кавычках стал тегом: {out!r}"
    assert "<code>&lt;br&gt;</code>" in out, (
        f"`<br>` в кавычках превратился в перенос строки: {out!r}"
    )


def test_markup_outside_code_still_works():
    """Антивакуум: литеральным становится только код, а не весь ответ."""
    out = to_telegram_html(ANSWER)
    assert "<h1>Вердикт: так отправить нельзя</h1>" in out, out
    assert "<b>переписать</b>" in out, out
    assert "<table" in out, f"таблица перестала быть таблицей: {out!r}"
