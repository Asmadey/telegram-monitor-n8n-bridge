"""Находки источника: схема, разбор ответа канала, сборка и отрисовка (13.9).

Итоговое сообщение перестаёт сочинять модель. Каждый канал отвечает данными по
схеме, а объединяет их, дедуплицирует и рисует КОД — поэтому результат
предсказуем, стоит один вызов на канал и не зависит от того, успеет ли модель
сгенерировать длинный текст за отведённое время.

Повод — два отказа за один день с одинаковым симптомом (JSON вместо
сообщения): склейка промптов и тайм-аут сведения. Общий корень у них один, и
он здесь убирается.

**Схема одна на три применения**, и в этом весь смысл: провайдеру она уходит в
`response_format`, ответ проверяется ею же, а контракт в промпте канала из неё
генерируется. Разойтись они не могут, потому что источник один.
"""

import json
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from app.services.telegram_markup import escape, unwrap_fence

# Сколько символов оставляем от поля. Модель иногда отвечает абзацем там, где
# просили строку, и такой ответ рвёт сообщение по длине у всех остальных.
MAX_FIELD_CHARS = 400


class Finding(BaseModel):
    """Одна находка.

    Все поля обязательные и строковые, пустая строка = «не указано». Так
    требует строгий режим структурного вывода: необязательное поле даёт в
    схеме `anyOf` с `null`, а его половина провайдеров не разбирает. Пустые
    поля отбрасывает отрисовка, а не разбор.

    `extra="ignore"`: модель дописывает свои поля (`relevance`, `score`), и
    чужое поле не повод выбрасывать находку.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str
    company: str
    salary: str
    format: str
    location: str
    description: str
    category: str
    post_url: str


class Findings(BaseModel):
    """Корень схемы — ОБЪЕКТ: массив верхним уровнем строгий режим не принимает."""

    model_config = ConfigDict(extra="ignore")

    findings: list[Finding]


# Что означает каждое поле — словами, потому что схема имён не объясняет.
_MEANING = {
    "title": "название вакансии",
    "company": "работодатель",
    "salary": "зарплата как написано в посте, без пересчётов",
    "format": "формат работы: удалённо, офис, гибрид",
    "location": "город или локация",
    "description": "суть одним предложением, до 220 символов",
    "category": "ваша категория находки",
    "post_url": "ссылка на исходный пост",
}


def _contract() -> str:
    """Контракт для промпта канала — ИЗ схемы, а не второй копией прозой.

    Пользователь описывает, ЧТО искать. Формат ответа — обязанность сервиса:
    промпт, который описывает формат сам, расходится со схемой в первый же
    день и молча ломает разбор.
    """
    fields = "\n".join(
        f"- {name}: {_MEANING.get(name, name)}"
        for name in Finding.model_json_schema()["properties"]
    )
    return (
        "Формат ответа задаёт сервис. Верни объект "
        '{"findings": [ ... ]}, где каждый элемент — найденное, со всеми '
        "полями:\n"
        f"{fields}\n"
        "Поле, которого в посте нет, оставь пустой строкой — не выдумывай и "
        "не пиши «не указано». Без ссылки находка не годится: такой элемент "
        "не возвращай вовсе."
    )


CHANNEL_CONTRACT = _contract()


def response_schema() -> dict:
    """Схема для `response_format` провайдера в строгом режиме.

    `additionalProperties: false` дописывается ЗДЕСЬ, а не задаётся моделью:
    `extra="forbid"` сделал бы строгой и нашу валидацию, а она обязана быть
    терпимой — модель дописывает свои поля (`relevance`, `score`), и чужое
    поле не повод выбросить находку. Строгость нужна провайдеру, терпимость —
    нам, и это не противоречие: разные стороны одного контракта.
    """
    schema = Findings.model_json_schema()
    schema["additionalProperties"] = False
    for definition in (schema.get("$defs") or {}).values():
        definition["additionalProperties"] = False
    return schema


def _first_json_block(text: str) -> str | None:
    """Первый сбалансированный `[...]` или `{...}` в тексте.

    Со счётчиком скобок и учётом строк, а не жадной регуляркой: `[` внутри
    описания вакансии закрывал бы массив раньше времени, а `.*` от первой
    скобки до последней склеивает два разных блока в один нечитаемый.
    """
    start = None
    opening = closing = ""
    depth = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if start is None:
            if char in "[{":
                start, opening = index, char
                closing = "]" if char == "[" else "}"
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_findings(text: str) -> tuple[list[dict], str]:
    """Находки из ответа канала и остаток, который разобрать не удалось.

    Разбор терпимый намеренно. Структурный вывод закрывает поломку JSON у
    провайдеров, которые его поддерживают, — но не у всех, и не при обрыве
    ответа по длине. Не разобралось — текст возвращается целиком: канал
    теряет структуру, но не находки.
    """
    raw = unwrap_fence(text or "").strip()
    if not raw:
        return [], ""
    block = _first_json_block(raw)
    if block is None:
        return [], raw
    try:
        loaded = json.loads(block)
    except ValueError:
        return [], raw
    if isinstance(loaded, dict):
        loaded = loaded.get("findings") or loaded.get("items") or []
    if not isinstance(loaded, list):
        return [], raw

    items: list[dict] = []
    for row in loaded:
        if not isinstance(row, dict):
            continue
        filled = {name: row.get(name, "") for name in Finding.model_fields}
        try:
            # Валидация — той же схемой, что ушла провайдеру. Кривой объект
            # выбрасывается ПОШТУЧНО: один сломанный не топит девятнадцать
            # целых.
            finding = Finding.model_validate(filled)
        except ValidationError:
            continue
        if not finding.post_url.strip():
            # Правило владельца: без ссылки находку нельзя сделать
            # кликабельной, а значит и показывать её незачем.
            continue
        items.append(
            {
                name: value[:MAX_FIELD_CHARS]
                for name, value in finding.model_dump().items()
            }
        )
    if not items and loaded:
        return [], raw
    return items, ""


def _key(item: dict) -> tuple[str, str]:
    """Ключ дубля, когда ссылки разные: тот же пост перепечатан в другом канале."""
    normalize = lambda value: re.sub(r"\s+", " ", (value or "").strip().lower())  # noqa: E731
    return normalize(item.get("title")), normalize(item.get("company"))


def _filled(item: dict) -> int:
    return sum(1 for value in item.values() if (value or "").strip())


def merge_findings(groups: list[dict]) -> list[dict]:
    """Находки всех каналов одним списком, без дублей, в порядке каналов.

    Дедупликация — то, ради чего конвейер вообще делили на разбор и сведение
    (11.4): одна вакансия висит в трёх каналах сразу. Промпт об этом просили,
    но просьба — не гарантия; здесь это делает код.
    """
    merged: list[dict] = []
    by_url: dict[str, int] = {}
    by_name: dict[tuple[str, str], int] = {}
    for group in groups:
        for item in group.get("items") or []:
            url = (item.get("post_url") or "").strip()
            index = by_url.get(url) if url else None
            if index is None:
                index = by_name.get(_key(item))
            if index is None:
                merged.append(dict(item))
                if url:
                    by_url[url] = len(merged) - 1
                by_name[_key(item)] = len(merged) - 1
                continue
            # При дубле остаётся более полная запись: канал, где пост
            # перепечатан коротко, не должен побеждать оригинал.
            if _filled(item) > _filled(merged[index]):
                merged[index] = dict(item)
    return merged


_ORDER = ("company", "salary", "format", "location")


def render_findings(
    items: list[dict],
    *,
    raw_blocks: list[tuple[str, str]] | None = None,
) -> str:
    """Сообщение из находок. Текст полей чужой — экранируется весь.

    Длину не режем: `dispatch._chunks` уже умеет это делать и знает лимиты
    обоих методов отправки.
    """
    lines: list[str] = []
    for item in items:
        title = escape(item.get("title") or "Без названия")
        url = (item.get("post_url") or "").strip()
        lines.append(f'<b><a href="{escape(url)}">{title}</a></b>')
        facts = [
            escape(item[name]) for name in _ORDER if (item.get(name) or "").strip()
        ]
        if facts:
            lines.append(" • ".join(facts))
        description = (item.get("description") or "").strip()
        if description:
            lines.append(escape(description))
        lines.append("")

    for title, text in raw_blocks or []:
        # Ответ не по схеме: находки дороже формата, текст идёт как есть.
        lines.append(f"<b>{escape(title)}</b>")
        lines.append(escape(text))
        lines.append("")
    return "\n".join(lines).strip()
