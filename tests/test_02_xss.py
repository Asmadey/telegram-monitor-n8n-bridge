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

Добавляя запись в SAFE_SINKS, пишите рядом, почему приёмник безопасен.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Поля, значения которых приходят из Telegram, от пользователя или от LLM.
RISKY_FIELDS = [
    "chat_title", "chat_username", "chat_target", "ai_analysis", "model_name",
    "event_type", "details", "sender", "prompt", "post_url", "photo_base64",
    "reactionsTitle", "name", "username", "title", "text", "status", "type",
]

# Вызовы, после которых значение безопасно вставлять в разметку.
ESCAPING_CALLS = ("esc(", "escapeHtml(", "formatTelegramText(",
                  "highlightText(", "encodeURIComponent(", "Number(")

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

# Сколько символов перед подстановкой просматривать в поисках приёмника.
SINK_LOOKBEHIND = 400


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
        elif ch == "?" and depth == 0 and expr[idx:idx + 2] not in ("?.", "??"):
            return expr[idx + 1:]
    return expr


def sink_before(src: str, pos: int) -> str | None:
    """Ближайший к подстановке приёмник: innerHTML или один из безопасных.

    Побеждает тот, что ближе — присваивание innerHTML, встреченное позже
    безопасного приёмника, означает, что мы уже в другом выражении.
    """
    window = src[max(0, pos - SINK_LOOKBEHIND):pos]
    best_marker, best_at = None, -1
    for marker in ("innerHTML", "insertAdjacentHTML", *SAFE_SINKS):
        at = window.rfind(marker)
        if at > best_at:
            best_marker, best_at = marker, at
    return best_marker


def violations_in(path: Path) -> list[str]:
    src = js_source(path.read_text(encoding="utf-8"),
                    is_html=path.suffix.lower() == ".html")
    found = []
    for m in INTERPOLATION.finditer(src):
        payload = output_part(m.group(1)).strip()
        if not FIELD_REF.search(payload):
            continue
        if any(call in payload for call in ESCAPING_CALLS):
            continue
        sink = sink_before(src, m.start())
        if sink in SAFE_SINKS:
            continue
        line = src.count("\n", 0, m.start()) + 1
        found.append(f"{path.name}:{line}  ${{{payload[:70]}}}   ← приёмник: {sink}")
    return found


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
    hits = [m.group(1) for m in INTERPOLATION.finditer(sample)
            if FIELD_REF.search(output_part(m.group(1)))]
    assert any("chat_title" in h for h in hits), "вложенная подстановка не найдена"


def test_apostrophes_and_regex_literals_do_not_blind_the_scan():
    """Метод не разбирает строки и регулярные литералы, поэтому не сбивается."""
    src = js_source(
        "<p>не удалось — don't panic</p>\n"
        "<script>\n"
        "  s = s.replace(/'/g, '&#39;');\n"
        "  el.innerHTML = `<div title=\"${m.chat_title}\"></div>`;\n"
        "</script>\n",
        is_html=True,
    )
    hits = [m.group(1) for m in INTERPOLATION.finditer(src)
            if FIELD_REF.search(output_part(m.group(1)))]
    assert any("chat_title" in h for h in hits)


def test_safe_sinks_are_not_reported():
    sample = "statusUser.textContent = `${data.user.username}`;"
    assert not [m for m in INTERPOLATION.finditer(sample)
                if sink_before(sample, m.start()) not in SAFE_SINKS]


def test_every_safe_sink_carries_a_justification():
    for marker, reason in SAFE_SINKS.items():
        assert reason.strip(), f"{marker}: не объяснено, почему приёмник безопасен"


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
