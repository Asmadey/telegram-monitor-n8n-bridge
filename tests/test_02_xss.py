"""Задача 0.4 — данные из Telegram не попадают в HTML без экранирования.

chat_title, имя диалога и поля журнала полностью контролируются посторонними
людьми: название канала задаёт его владелец, отображаемое имя — любой, кто
написал оператору. Всё это интерполировалось в innerHTML напрямую.

О методе. Три первые версии этого теста пытались разобрать JS-шаблонные строки
самодельным лексером и все три показывали зелёный при живом XSS: сначала
регулярка обрывалась на вложенном шаблоне, потом разбор .html сбивался на
апострофах в русском тексте, потом лексер уезжал на регулярном литерале
`/'/g`. Вывод: не парсить JS. Здесь проверяются ВСЕ подстановки ${...}, а
безопасные приёмники (textContent, .href, fetch, showToast) перечислены явным
списком. Ложноположительное срабатывание стоит одной строки в SAFE_SINKS и
видно в ревью; ошибка лексера — молчаливая слепота на весь файл.

Задача 5.2 добавила билдер html` (render.js): он экранирует каждую подстановку
сам, поэтому маркер html` — безопасный приёмник, а payload внутри его шаблона
не обязан нести свой esc. Но raw() отключает экранирование целиком: маркер
raw( читается как НЕБЕЗОПАСНЫЙ приёмник — подстановка в его области обязана
содержать экранирующий вызов по-прежнему (побеждает ближайший маркер).

Добавляя запись в SAFE_SINKS, пишите рядом, почему приёмник безопасен.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Поля, значения которых приходят из Telegram, от пользователя или от LLM.
RISKY_FIELDS = [
    "chat_title",
    "chat_username",
    "chat_target",
    "ai_analysis",
    "model_name",
    "event_type",
    "details",
    "sender",
    "prompt",
    "post_url",
    "photo_base64",
    "reactionsTitle",
    "name",
    "username",
    "title",
    "text",
    "status",
    "type",
]

# Вызовы, после которых значение безопасно вставлять в разметку.
ESCAPING_CALLS = (
    "esc(",
    "escapeHtml(",
    "formatTelegramText(",
    "highlightText(",
    "encodeURIComponent(",
    "Number(",
)

# Приёмники, которые не разбирают HTML. Ключ — маркер в коде, значение — причина.
SAFE_SINKS = {
    ".textContent": "textContent вставляет текстовый узел, разметка не парсится",
    ".placeholder": "атрибут задаётся через DOM-свойство, не через разметку",
    ".value": "то же самое — DOM-свойство поля ввода",
    ".href": "DOM-свойство ссылки; префикс схемы задан в коде литералом",
    "showToast(": "showToast присваивает toastMsg.textContent",
    "fetch(": "строка уходит в URL запроса, а не в документ",
    "new RegExp(": "строка компилируется в регулярное выражение",
    "console.": "вывод в консоль разработчика",
}

SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
INTERPOLATION = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", re.S)
FIELD_REF = re.compile(r"\b(?:" + "|".join(RISKY_FIELDS) + r")\b")

# Задача 5.2: билдер html` (render.js) экранирует все подстановки сам.
BUILDER_TAG = "html`"
# raw() — осознанный opt-out из экранирования билдера: в его области
# значение уходит в разметку как есть, маркер НЕБЕЗОПАСЕН.
RAW_MARKER = "raw("

# Сколько символов перед подстановкой просматривать в поисках приёмника.
SINK_LOOKBEHIND = 400
# Глубокая позиция в длинном шаблоне: 400 символов может не хватить до
# открывающего html` — расширяем окно только ради поиска билдера.
BUILDER_LOOKBEHIND = 4000


def js_source(text: str, is_html: bool) -> str:
    """Для .html оставляет только содержимое <script>, сохраняя нумерацию строк."""
    if not is_html:
        return text
    out: list[str] = []
    pos = 0
    for m in SCRIPT_BLOCK.finditer(text):
        if not m.group(1).strip():
            continue
        out.append("\n" * text.count("\n", pos, m.start(1)))
        out.append(m.group(1))
        pos = m.end(1)
    out.append("\n" * text.count("\n", pos, len(text)))
    return "".join(out)


def output_part(expr: str) -> str:
    """Отбрасывает условие тернарника: `cond ? a : b` → всё после '?'.

    Ссылка на поле в условии ничего не печатает — экранировать её не нужно.
    """
    depth = 0
    for idx, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0 and expr[idx : idx + 2] not in ("?.", "??"):
            return expr[idx + 1 :]
    return expr


def _nearest_marker_before(text: str, pos: int, markers: tuple[str, ...]) -> str | None:
    best, best_at = None, -1
    for marker in markers:
        at = text.rfind(marker, 0, pos)
        if at > best_at:
            best, best_at = marker, at
    return best


def sink_before(src: str, pos: int) -> str | None:
    """Ближайший к подстановке приёмник.

    Побеждает тот, что ближе — присваивание innerHTML, встреченное позже
    безопасного приёмника, означает, что мы уже в другом выражении.
    Если в окне нет ни одного маркера (глубокая позиция в длинном шаблоне
    билдера), ищем открывающий html` в широком окне; иначе — None.
    """
    window = src[max(0, pos - SINK_LOOKBEHIND) : pos]
    best_marker = _nearest_marker_before(
        window,
        len(window),
        ("innerHTML", "insertAdjacentHTML", BUILDER_TAG, *SAFE_SINKS),
    )
    if best_marker is None:
        wide = src[max(0, pos - BUILDER_LOOKBEHIND) : pos]
        if BUILDER_TAG in wide:
            return BUILDER_TAG
    return best_marker


def raw_payload_safe(payload: str, outer_sink: str | None) -> bool:
    """payload содержит raw(...) — неэкранированную область внутри самой
    подстановки. raw-область не выходит за пределы своего выражения, поэтому
    окно снаружи тут не судья: каждая ссылка на рискованное поле внутри
    payload обязана быть закрыта экранирующим вызовом (raw(esc(..))) или
    шаблоном билдера (raw(html`..`)). Поле ДО raw-области судит приёмник
    снаружи.
    """
    for fm in FIELD_REF.finditer(payload):
        if re.match(r"\s*=", payload[fm.end() :]):
            # `title="` внутри разметки — имя HTML-атрибута, а не ссылка
            # на поле; выражение в подстановке не начинается с `=`
            continue
        nearest = _nearest_marker_before(
            payload, fm.start(), (*ESCAPING_CALLS, RAW_MARKER, BUILDER_TAG)
        )
        if nearest == RAW_MARKER:
            return False
        if nearest is None and not (
            outer_sink in SAFE_SINKS or outer_sink == BUILDER_TAG
        ):
            return False
    return True


def violations_in_src(src: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for m in INTERPOLATION.finditer(src):
        payload = output_part(m.group(1)).strip()
        if not FIELD_REF.search(payload):
            continue
        sink = sink_before(src, m.start())
        if RAW_MARKER in payload:
            if raw_payload_safe(payload, sink):
                continue
            line = src.count("\n", 0, m.start()) + 1
            found.append((line, payload, RAW_MARKER))
            continue
        if any(call in payload for call in ESCAPING_CALLS):
            continue
        if sink in SAFE_SINKS or sink == BUILDER_TAG:
            continue
        line = src.count("\n", 0, m.start()) + 1
        found.append((line, payload, sink))
    return found


def violations_in(path: Path) -> list[str]:
    src = js_source(
        path.read_text(encoding="utf-8"), is_html=path.suffix.lower() == ".html"
    )
    return [
        f"{path.name}:{line}  ${{{payload[:70]}}}   ← приёмник: {sink}"
        for line, payload, sink in violations_in_src(src)
    ]


def static_files() -> list[Path]:
    out: list[Path] = []
    for pattern in ("*.html", "*.js"):
        out += sorted((ROOT / "static").rglob(pattern))
    return out


# --------------------------------------------------------------------------
# Страховка на сам метод: каждая проверка соответствует реальному промаху,
# из-за которого прошлые версии теста показывали зелёный при живом XSS.
# --------------------------------------------------------------------------


def test_detects_interpolation_inside_a_nested_template():
    sample = "el.innerHTML = `<div>${a ? `<b>${m.chat_title}</b>` : ''}</div>`;"
    hits = [
        m.group(1)
        for m in INTERPOLATION.finditer(sample)
        if FIELD_REF.search(output_part(m.group(1)))
    ]
    assert any("chat_title" in h for h in hits), "вложенная подстановка не найдена"


def test_apostrophes_and_regex_literals_do_not_blind_the_scan():
    """Метод не разбирает строки и регулярные литералы, поэтому не сбивается."""
    src = js_source(
        "<p>не удалось — don't panic</p>\n"
        "<script>\n"
        "  s = s.replace(/'/g, '&#39;');\n"
        '  el.innerHTML = `<div title="${m.chat_title}"></div>`;\n'
        "</script>\n",
        is_html=True,
    )
    hits = [
        m.group(1)
        for m in INTERPOLATION.finditer(src)
        if FIELD_REF.search(output_part(m.group(1)))
    ]
    assert any("chat_title" in h for h in hits)


def test_safe_sinks_are_not_reported():
    sample = "statusUser.textContent = `${data.user.username}`;"
    assert not [
        m
        for m in INTERPOLATION.finditer(sample)
        if sink_before(sample, m.start()) not in SAFE_SINKS
    ]


def test_every_safe_sink_carries_a_justification():
    for marker, reason in SAFE_SINKS.items():
        assert reason.strip(), f"{marker}: не объяснено, почему приёмник безопасен"


def test_builder_tag_is_a_safe_sink():
    """Задача 5.2: html` экранирует каждую подстановку сам — payload
    внутри шаблона билдера не обязан нести свой экранирующий вызов."""
    sample = 'el.innerHTML = html`<div title="${m.chat_title}">${m.text}</div>`;'
    assert violations_in_src(sample) == []


def test_raw_region_inside_builder_is_still_unsafe():
    """raw() отключает экранирование билдера: подстановка внутри его
    области обязана содержать экранирующий вызов, иначе — нарушение."""
    sample = "el.innerHTML = html`<div>${raw(`<b>${m.chat_title}</b>`)}</div>`;"
    hits = [payload for _, payload, _ in violations_in_src(sample)]
    assert any("chat_title" in h for h in hits)


def test_raw_wrapping_a_builder_is_safe():
    """raw(html`...`) — наоборот, безопасно: подстановка уходит в
    экранирующий шаблон, raw лишь вставляет готовый HTML."""
    sample = (
        'el.innerHTML = html`<div>${raw(html`<a href="${m.post_url}">x</a>`)}</div>`;'
    )
    assert violations_in_src(sample) == []


def test_deep_position_falls_back_to_builder_lookup():
    """400 символов может не хватить до открывающего html` в длинном
    шаблоне — fallback ищет билдер шире; это не слепота, а тот же
    маркер, просто дальше."""
    filler = "x" * 500
    sample = "el.innerHTML = html`<div>" + filler + "${m.chat_title}</div>`;"
    assert violations_in_src(sample) == []


# --------------------------------------------------------------------------
# Собственно проверка
# --------------------------------------------------------------------------


def test_risky_fields_are_escaped_before_reaching_html():
    violations: list[str] = []
    for path in static_files():
        violations += violations_in(path)
    assert not violations, (
        f"Неэкранированные подстановки в HTML ({len(violations)} шт.):\n"
        + "\n".join(violations)
    )
