"""Задача 5.2 — экранирование по умолчанию (PLAN.md).

В render.js — единственная функция построения HTML, которая экранирует
ВСЕ подстановки: tagged-template `html` (каждая ${...} проходит esc),
raw() — осознанный opt-out для готового безопасного HTML (виден в ревью).
Прямой innerHTML со строковой интерполяцией запрещается; XSS-сканер
задачи 0.4 (test_02) признаёт html` безопасным приёмником и продолжает
гарантировать, что сырые поля не попадают в разметку без экранирования.

Уровни проверок:
- поведенческий: node импортирует render.js и зовёт билдер — живой
  payload `<img src=x onerror=...>` обязан выйти экранированным
  (без браузера, но на настоящем исполнении, не на regex-вере);
- структурный: innerHTML/insertAdjacentHTML без тега html` запрещены.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"

# Строка с присваиванием в HTML-приёмник обязана строить разметку только
# билдером: каждый шаблонный литерал на ней помечен html`.
INNER_HTML_LINE = re.compile(r"\.(?:innerHTML|insertAdjacentHTML)")


def _js_files() -> list[Path]:
    return sorted(JS_DIR.glob("*.js"))


def _node() -> str | None:
    return shutil.which("node")


def test_html_builder_escapes_every_interpolation():
    """Билдер html экранирует каждую подстановку сам — по умолчанию,
    без участия вызывающего. Проверяем НАСТОЯЩИМ исполнением в node
    (без браузера): payload с тегом и кавычками обязан выйти
    экранированным; raw() — единственный путь пройти как есть."""
    node = _node()
    if node is None:
        pytest.skip("node недоступен — билдер не исполнить")

    script = """
import { html, raw } from "./static/js/render.js";

const evilTag = `<img src=x onerror=alert(1)>`;
const evilQuote = `q" onclick='bad()'`;

const out = html`<a href="u" title="${evilQuote}">${evilTag}</a>`;
if (out.includes("<img")) throw new Error("тег не экранирован: " + out);
// экранирование нейтрализует тег, а не вырезает слово: "onerror" в
// выводе легитимен как текст (&lt;img src=x onerror=alert(1)&gt;).
// Опасно только живое вхождение — атрибут внутри настоящего тега.
if (/<\\S+[^>]*onerror/i.test(out)) throw new Error("живой onerror: " + out);
if (out.includes('" onclick')) throw new Error("кавычка атрибута жива: " + out);
if (!out.includes("&lt;img")) throw new Error("нет &lt;: " + out);

const safe = html`<b>${raw(`<i>готовый</i>`)}</b>`;
if (!safe.includes("<i>готовый</i>")) throw new Error("raw не проходит: " + safe);

const num = html`<td>${7}</td>`;
if (!num.includes(">7<")) throw new Error("число сломано: " + num);

console.log("OK");
"""
    proc = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, (
        f"билдер html не работает / не экранирует:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout


def test_innerhtml_only_through_builder():
    r"""Прямой innerHTML со строковой интерполяцией запрещён: открывающий
    бэктик шаблона на строке с HTML-приёмником обязан идти после тега
    билдера (html`) или после raw( — осознанного opt-out. Закрывающий
    бэктик отличаем по чётности: он всегда нечётный по счёту среди
    предыдущих бэктиков строки (мы «внутри» шаблона). Наивное правило
    «каждый бэктик перед html» ложно срабатывало бы на закрывающих
    кавычках вида ...${raw(x)}\`;."""
    violations = []
    for path in _js_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not INNER_HTML_LINE.search(line) or "`" not in line:
                continue
            for m in re.finditer(r"`", line):
                before = line[: m.start()]
                inside_template = before.count("`") % 2 == 1
                if inside_template:
                    continue  # закрывающая кавычка шаблона/сырой вставки
                if before.rstrip().endswith("html") or before.rstrip().endswith("raw("):
                    continue  # открывающая кавычка тегированного шаблона/raw(
                violations.append(
                    f"{path.name}:{lineno}  шаблон без тега html`/raw( → {line.strip()[:70]}"
                )
                break
    assert violations == [], (
        "innerHTML мимо билдера html` (экранирование не по умолчанию):\n  "
        + "\n  ".join(violations)
    )
