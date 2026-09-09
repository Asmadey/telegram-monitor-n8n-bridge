"""Разметка ответа модели → HTML, который понимает Bot API (задача 9.17).

Доставка шлёт сообщения с `parse_mode=HTML`, а модель отвечает тем, чего
попросил промпт. У одного канала промпт на 4311 символов прямо требует
HTML, у остальных промпт пуст — и оттуда приходит Markdown: `**жирный**`,
`` `код` ``, `### заголовок`. Telegram показывает это как есть, со всеми
звёздочками.

Чинить правкой промптов нельзя. Промпт — не гарантия: модель отклоняется
от него на длинных ответах, меняется между версиями, а писать промпты
будут пользователи, которым про подмножество HTML в Bot API знать неоткуда.
Формат ответа — обязанность доставки.

Второе, ради чего этот модуль существует, — экранирование. Пост со знаком
`<` ломал разбор, Bot API отвечал 400, доставка повторяла запрос без
`parse_mode`, и сообщение уходило целиком без оформления. Один символ в
чужом посте незаметно отключал разметку всему сообщению.
"""

import html
import re

# Теги, которые Bot API понимает. Всё остальное — текст, а не разметка.
ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "tg-spoiler",
    }
)

_TAG = re.compile(r"</?(?P<name>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>\s[^<>]*)?/?>")
_BR = re.compile(r"<br\s*/?>", re.I)
_HREF = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.I)
_SAFE_SCHEME = re.compile(r"^(https?://|tg://)", re.I)

_FENCE = re.compile(r"```[a-zA-Z0-9_+.-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD_ALT = re.compile(r"__(.+?)__", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_ITALIC_ALT = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_HEADER = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*(.+?)[ \t]*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

_PLACEHOLDER = "\x00{}\x00"


def _keep_tag(match: re.Match) -> str | None:
    """Разрешённый тег остаётся собой; у ссылки выживает только href."""
    name = match.group("name").lower()
    if name not in ALLOWED_TAGS:
        return None
    raw = match.group(0)
    if name != "a" or raw.startswith("</"):
        # у остальных тегов атрибуты Bot API игнорирует — не тащим их дальше
        return f"</{name}>" if raw.startswith("</") else f"<{name}>"
    href = _HREF.search(raw)
    if href is None or not _SAFE_SCHEME.match(href.group(1)):
        # ссылка без адреса или с чужой схемой — это уже не ссылка
        return "<a>" if href is None else None
    return f'<a href="{html.escape(href.group(1), quote=True)}">'


def _markdown(text: str) -> str:
    """Markdown → теги. Вход уже экранирован, выход содержит только наши теги."""
    stash: list[str] = []

    def keep(rendered: str) -> str:
        stash.append(rendered)
        return _PLACEHOLDER.format(len(stash) - 1)

    # Код — первым: внутри него Markdown не разметка, а буквальный текст
    text = _FENCE.sub(lambda m: keep(f"<pre>{m.group(1)}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)

    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _HEADER.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_ALT.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _ITALIC_ALT.sub(r"<i>\1</i>", text)

    for index, rendered in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), rendered)
    return text


def escape(text: str) -> str:
    """Чужой текст (заголовок канала, чужой пост) — всегда только текст."""
    return html.escape(text or "", quote=False)


def to_telegram_html(text: str) -> str:
    """Привести ответ модели к разметке, которую разберёт Bot API."""
    if not text:
        return ""
    text = _BR.sub("\n", text)

    parts: list[str] = []
    position = 0
    # Готовый HTML (промпт Finder.work требует именно его) проходит насквозь;
    # всё между тегами экранируется и разбирается как Markdown. Внутри
    # <code>/<pre> Markdown не трогаем — там он буквальный текст.
    depth_literal = 0
    for match in _TAG.finditer(text):
        kept = _keep_tag(match)
        if kept is None:
            continue
        chunk = text[position : match.start()]
        parts.append(escape(chunk) if depth_literal else _markdown(escape(chunk)))
        parts.append(kept)
        position = match.end()
        name = match.group("name").lower()
        if name in ("code", "pre"):
            depth_literal += -1 if match.group(0).startswith("</") else 1
            depth_literal = max(depth_literal, 0)

    tail = text[position:]
    parts.append(escape(tail) if depth_literal else _markdown(escape(tail)))
    return "".join(parts)
