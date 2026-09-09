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
#
# Набор зависит от метода отправки. `sendRichMessage` (Bot API 10.1) знает
# таблицы, заголовки и списки; `sendMessage` — нет, и для него та же
# сводка разворачивается в строки (см. rich_html_to_plain).
PLAIN_TAGS = frozenset(
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
RICH_ONLY_TAGS = frozenset(
    {
        "table",
        "caption",
        "tr",
        "th",
        "td",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "mark",
    }
)
ALLOWED_TAGS = PLAIN_TAGS | RICH_ONLY_TAGS

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
_HEADER = re.compile(r"(?m)^[ \t]*(#{1,6})[ \t]*(.+?)[ \t]*$")
_LIST = re.compile(r"(?m)(?:^[ \t]*[-*+][ \t]+.+$\n?)+")
_LIST_ITEM = re.compile(r"(?m)^[ \t]*[-*+][ \t]+(.+?)[ \t]*$")
# Markdown-таблица: шапка, строка-разделитель, тело. Именно её присылает
# модель, когда её просят «сведи по каждой вакансии».
_TABLE = re.compile(
    r"(?m)^[ \t]*\|(?P<head>.+?)\|[ \t]*\n"
    r"[ \t]*\|(?P<rule>[ \t:\-|]+)\|[ \t]*\n"
    r"(?P<body>(?:[ \t]*\|.*\|[ \t]*(?:\n|$))*)"
)
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

    # Таблица — блочная и должна разбираться ДО строчных правил: иначе
    # `**жирный**` внутри ячейки разъедет по границам тега.
    text = _TABLE.sub(lambda m: keep(_render_table(m)), text)
    text = _LIST.sub(lambda m: keep(_render_list(m.group(0))), text)

    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    # Заголовок — настоящим тегом: `sendRichMessage` их знает, и уровень
    # видно глазом, а не только жирностью.
    text = _HEADER.sub(
        lambda m: (
            f"<h{min(len(m.group(1)), 6)}>{m.group(2)}</h{min(len(m.group(1)), 6)}>"
        ),
        text,
    )
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_ALT.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _ITALIC_ALT.sub(r"<i>\1</i>", text)

    for index, rendered in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), rendered)
    return text


def _cells(line: str) -> list[str]:
    """Ячейки одной строки Markdown-таблицы."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(match: "re.Match[str]") -> str:
    """Markdown-таблица → таблица Bot API.

    `bordered striped compact` — не украшение: на телефоне сводка из шести
    колонок без границ и зебры нечитаема, а compact добавили в 10.3 именно
    для узких экранов.
    """
    header = _cells(match.group("head"))
    rows = [
        _cells(line)
        for line in match.group("body").splitlines()
        if line.strip().startswith("|")
    ]
    parts = ["<table bordered striped compact>"]
    parts.append("<tr>" + "".join(f"<th>{c}</th>" for c in header) + "</tr>")
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def _render_list(block: str) -> str:
    items = _LIST_ITEM.findall(block)
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


# Разворот богатой разметки в ту, что понимает sendMessage. Нужен запасному
# пути: таблица, ушедшая чёрточками, хуже отсутствия таблицы.


def rich_html_to_plain(rich: str) -> str:
    """Свести таблицы в строки, заголовки — в жирный, списки — в маркеры."""
    text = re.sub(r"</t[dh]>\s*<t[dh][^<>]*>", " — ", rich)
    text = re.sub(r"</tr>\s*<tr[^<>]*>", "\n", text)
    text = re.sub(r"</?(table|caption|tbody|thead)[^<>]*>", "\n", text)
    text = re.sub(r"</?tr[^<>]*>|</?t[dh][^<>]*>", "", text)
    text = re.sub(r"<h[1-6][^<>]*>", "<b>", text)
    text = re.sub(r"</h[1-6]>", "</b>", text)
    text = re.sub(r"</?(ul|ol)[^<>]*>", "\n", text)
    text = re.sub(r"<li[^<>]*>", "• ", text)
    text = text.replace("</li>", "\n")
    text = re.sub(r"</?mark[^<>]*>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_plain_html(text: str) -> str:
    """Разметка для `sendMessage`: тот же разбор, но без богатых тегов."""
    return rich_html_to_plain(to_telegram_html(text))


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
