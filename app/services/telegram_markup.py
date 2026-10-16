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
        # Абзац, разделитель и части таблицы — из того же списка документации
        # (13.12). Без них промпт, написанный ПО документации Telegram,
        # привозил в сообщение буквальные `<p>` и `</p>`: неизвестный тег у
        # нас экранируется, а не выбрасывается.
        "p",
        "hr",
        "thead",
        "tbody",
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

# Перенос строки в богатом сообщении делает тег, а не символ: поле `html`
# метода `sendRichMessage` разбирается как настоящий HTML, где перевод строки
# — обычный пробел. Документация Bot API говорит это прямым текстом, строкой
# под списком строчных тегов: «all the text above was on the same line».
_BLOCK = "table|caption|thead|tbody|tr|th|td|ul|ol|li|h[1-6]|pre|blockquote|hr|p"
_NEWLINE_AFTER_BLOCK = re.compile(rf"(</?(?:{_BLOCK})>)[ \t]*\n+")
_NEWLINE_BEFORE_BLOCK = re.compile(rf"\n+[ \t]*(</?(?:{_BLOCK})>)")
_PRE_BLOCK = re.compile(r"<pre>.*?</pre>", re.S)
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


# Обёртка, в которую модель кладёт ВЕСЬ ответ: «```html … ```». Промпт её не
# просит — так отвечают модели, когда их просят «оформи в HTML». Ограждения
# проходили разбор насквозь и приезжали в Telegram буквами: текст режется по
# настоящим тегам, и пара ``` оказывается в разных кусках, то есть не
# находится никогда (найдено владельцем на источнике «VASILE LUNGO»).
# Язык обёртки: пустой или «разметочный». Именно им модель помечает ответ,
# который решила «показать» целиком. Имя настоящего языка (python, bash, sql)
# — заявка на ПРИМЕР КОДА, и такой ответ остаётся кодом: этого требует
# `test_105`, и требует справедливо.
_ANSWER_TAGS = frozenset({"", "html", "json", "markdown", "md", "text", "txt"})
_WRAPPED = re.compile(
    r"\A\s*```(?P<tag>[\w+-]*)[ \t]*\r?\n(?P<body>.*?)\r?\n?\s*```\s*\Z", re.S
)
# Незакрытая обёртка: модель начала «```html» и не закрыла. Язык здесь важен —
# «```html» это заявка на разметку ответа, а «```python» на пример кода.
_WRAPPED_OPEN = re.compile(r"\A\s*```html[ \t]*\r?\n(?P<body>.*)\Z", re.S | re.I)


def unwrap_fence(text: str) -> str:
    """Снять ОДНО ограждение, обнимающее весь ответ.

    Ограждение внутри ответа — пример кода, и оно остаётся кодом: снимается
    только то, что обнимает ответ целиком и внутри себя других ограждений не
    содержит.

    Язык решает. Пустой, `html`, `json`, `markdown` — так модель помечает
    ответ, который решила «показать» целиком. Имя настоящего языка
    (`python`, `bash`) — заявка на пример кода, и ответ из одного такого
    блока остаётся кодом.
    """
    match = _WRAPPED.match(text or "")
    if (
        match
        and match.group("tag").lower() in _ANSWER_TAGS
        and "```" not in match.group("body")
    ):
        return match.group("body")
    match = _WRAPPED_OPEN.match(text or "")
    if match and "```" not in match.group("body"):
        return match.group("body")
    return text


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

    # Заголовок — настоящим тегом: `sendRichMessage` их знает, и уровень
    # видно глазом, а не только жирностью.
    text = _HEADER.sub(
        lambda m: (
            f"<h{min(len(m.group(1)), 6)}>{m.group(2)}</h{min(len(m.group(1)), 6)}>"
        ),
        text,
    )
    text = _inline(text)

    for index, rendered in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), rendered)
    return text


def _inline(text: str) -> str:
    """Строчная разметка: ссылки, жирный, курсив, зачёркнутый.

    Вынесена из `_markdown`, потому что список и таблица убираются в заглушку
    раньше строчных правил — и без этого `- [Вакансия](https://…)` приезжал
    буквами, хотя та же ссылка вне списка становилась ссылкой (13.23).
    """
    # Вход экранирован с `quote=False`: кавычка из адреса дошла бы до `href`
    # сырой и открыла бы новый атрибут — `[x](https://a"onmouseover="…)`.
    # В Telegram это ломало только разметку, в браузере кабинета — XSS (13.20).
    text = _LINK.sub(
        lambda m: f'<a href="{_quote_attr(m.group(2))}">{m.group(1)}</a>', text
    )
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_ALT.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return _ITALIC_ALT.sub(r"<i>\1</i>", text)


def _cells(line: str) -> list[str]:
    """Ячейки одной строки Markdown-таблицы."""
    return [_inline(cell.strip()) for cell in line.strip().strip("|").split("|")]


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
    return "<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>"


# Разворот богатой разметки в ту, что понимает sendMessage. Нужен запасному
# пути: таблица, ушедшая чёрточками, хуже отсутствия таблицы.


def rich_html_to_plain(rich: str) -> str:
    """Свести таблицы в строки, заголовки — в жирный, списки — в маркеры.

    Первым — `<br>`: в наборе тегов `sendMessage` его нет вовсе, и для него
    перенос строки снова обычный `\n`. Неизвестный тег на этом пути — 400 и
    потеря оформления у всей сводки, а не у одной строки.
    """
    text = _BR.sub("\n", rich)
    text = re.sub(r"</t[dh]>\s*<t[dh][^<>]*>", " — ", text)
    text = re.sub(r"</tr>\s*<tr[^<>]*>", "\n", text)
    text = re.sub(r"</?(table|caption|tbody|thead)[^<>]*>", "\n", text)
    # Абзац и разделитель: `sendMessage` их не знает, а пустая строка знает.
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<p[^<>]*>|<hr\s*/?>", "\n", text)
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


def _quote_attr(value: str) -> str:
    """Дописать экранирование кавычек к тексту, уже прошедшему `escape`.

    Повторный `html.escape` удвоил бы `&amp;`; здесь не хватает только кавычек.
    """
    return value.replace('"', "&quot;").replace("'", "&#x27;")


# Ссылки, которые выпускает сам санитайзер: `_keep_tag` и `_markdown` пишут
# `<a href="…">` ровно в таком виде, чужих атрибутов у них не бывает.
_OWN_LINK = re.compile(r'<a href="([^"<>]*)">')
_ANY_TAG = re.compile(r"<[^<>]*>")
_TAG_SPLIT = re.compile(r"(<[^<>]*>)")


def to_cabinet_html(text: str) -> str:
    """Сводка для кабинета: ТОТ ЖЕ разбор, что для бота (13.20).

    Отдельного санитайзера у кабинета нет намеренно: два разбора одного текста
    однажды разойдутся, и в бот уйдёт одно, а в кабинете покажется другое.
    Сверху добавляется только то, чего требует браузер: ссылка открывается в
    новой вкладке и не получает доступа к окну кабинета.
    """
    rich = _OWN_LINK.sub(
        r'<a href="\1" rel="noopener noreferrer" target="_blank">',
        to_telegram_html(text),
    )
    # Кавычки в ТЕКСТЕ безвредны, но экранируются и там: отклонённый тег
    # чужой ссылки остаётся видимым текстом, и в нём не должно оставаться
    # ничего, что читается как готовый атрибут, — если такой текст однажды
    # окажется не на своём месте, он всё равно останется текстом.
    return "".join(
        part if part.startswith("<") else _quote_attr(part)
        for part in _TAG_SPLIT.split(rich)
    )


def to_cabinet_text(text: str) -> str:
    """Сводка простым текстом — для сниппета карточки и кнопки «Скопировать».

    Теги сняты, сущности раскрыты: экранирует такой текст уже тот, кто
    вставляет его в страницу (билдер `html` в кабинете).
    """
    plain = _ANY_TAG.sub("", rich_html_to_plain(to_telegram_html(text)))
    return html.unescape(plain).strip()


def _rich_line_breaks(rich: str) -> str:
    """Перевод строки → `<br>`; структурный перевод строки — в никуда.

    Три случая разные. Внутри `<pre>` перенос делает сам пробел, и тег там
    лишний. Между блочными тегами (`</li>` и `<li>`, конец таблицы и
    следующий абзац) перевод строки — отступ исходника: превратить его в
    `<br>` значит вставить пустую строку после каждого пункта списка.
    Всё остальное — настоящий перенос, который человек и ждёт увидеть.
    """
    if not rich:
        return rich
    kept: list[str] = []

    def stash(match: "re.Match[str]") -> str:
        kept.append(match.group(0))
        return _PLACEHOLDER.format(len(kept) - 1)

    rich = _PRE_BLOCK.sub(stash, rich).strip("\n")
    rich = re.sub(r"\n{3,}", "\n\n", rich)
    rich = _NEWLINE_AFTER_BLOCK.sub(r"\1", rich)
    rich = _NEWLINE_BEFORE_BLOCK.sub(r"\1", rich)
    rich = rich.replace("\n", "<br>")
    for index, block in enumerate(kept):
        rich = rich.replace(_PLACEHOLDER.format(index), block)
    return rich


def _text(chunk: str) -> str:
    """Текст между тегами — к буквам, прежде чем экранировать.

    Модель отвечает настоящим HTML, и амперсанд в нём уже записан сущностью
    `&amp;`. Экранировать такой текст сразу значило получить `&amp;amp;` —
    пользователь видел «P&amp;L» и в боте, и в кабинете (13.23). Раскрытая
    сущность тут же экранируется заново, поэтому `&lt;script&gt;` остаётся
    текстом, а не становится тегом.
    """
    return html.unescape(chunk)


def to_telegram_html(text: str) -> str:
    """Привести ответ модели к разметке, которую разберёт Bot API."""
    if not text:
        return ""
    text = unwrap_fence(text)
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
        chunk = _text(text[position : match.start()])
        parts.append(escape(chunk) if depth_literal else _markdown(escape(chunk)))
        parts.append(kept)
        position = match.end()
        name = match.group("name").lower()
        if name in ("code", "pre"):
            depth_literal += -1 if match.group(0).startswith("</") else 1
            depth_literal = max(depth_literal, 0)

    tail = _text(text[position:])
    parts.append(escape(tail) if depth_literal else _markdown(escape(tail)))
    return _rich_line_breaks("".join(parts))
