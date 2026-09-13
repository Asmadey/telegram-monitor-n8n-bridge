"""Расход виден в интерфейсе, а не только в API (задача 12.10).

Задача 12.7 завела разрез расхода по источникам и каналам и отдала его
`GET /api/usage`. На этом вопрос «какой канал жжёт бюджет» остался отвечен
только для того, кто умеет звать API руками, — то есть для одного человека
в проекте. Данные, до которых не дойти из интерфейса, решений не меняют.

Разметку строит отдельный модуль `usage.js`, и его главная функция —
чистая: данные на входе, строка разметки на выходе. Поэтому проверяется
она НАСТОЯЩИМ исполнением в node, а не разбором исходника: вопрос «что
увидит человек при пустом расходе» на регулярках не решается.

Отдельно закреплено поведение при нуле. Пустой расход — законное, частое
состояние (месяц только начался, AI выключен), и показывать в этом случае
пустое место значит повторить ошибку всей фазы 9: «ничего не потрачено» и
«не смогли посмотреть» обязаны выглядеть по-разному.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS_DIR = ROOT / "static" / "js"
USAGE_JS = JS_DIR / "usage.js"


def _run(body: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — разметку не исполнить")
    return subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            "import { usageMarkup } from './static/js/usage.js';\n" + body,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


# --------------------------------------------------------------------------
# Разметка и подключение
# --------------------------------------------------------------------------


def test_the_module_exists_and_is_loaded():
    assert USAGE_JS.exists(), "нет модуля static/js/usage.js"
    main = (JS_DIR / "main.js").read_text(encoding="utf-8")
    assert "usage.js" in main, "модуль расхода никуда не подключён"


def test_the_tab_has_a_place_for_the_report():
    assert 'id="usageBody"' in INDEX, "в разметке нет места под расход"


def test_the_report_is_refreshed_when_the_tab_opens():
    """Открытая вкладка обязана показывать сегодняшнее, а не вчерашнее."""
    main = (JS_DIR / "main.js").read_text(encoding="utf-8")
    integration = main[main.index("tabId === 'integration'") :][:400]
    assert "loadUsage" in integration, (
        "расход не обновляется при открытии вкладки: отчёт покажет то, что "
        "успело загрузиться когда-то раньше"
    )


def test_no_inline_handlers_in_the_new_markup():
    """CSP `script-src 'self'`: инлайновый обработчик молча не исполнится."""
    block = INDEX[
        INDEX.index('id="usageBody"') - 800 : INDEX.index('id="usageBody"') + 400
    ]
    assert not re.search(r"\son(click|change|input|submit)=", block), (
        "инлайновый обработчик в блоке расхода — под CSP он мёртв"
    )


# --------------------------------------------------------------------------
# Поведение разметки
# --------------------------------------------------------------------------


def test_zero_spend_says_so_instead_of_showing_nothing():
    proc = _run("""
const out = usageMarkup({ period: '2026-09', total: 0, limit: 1000, outside_sources: 0, sources: [] });
if (!out.trim()) throw new Error('при нулевом расходе интерфейс пуст — неотличимо от поломки');
if (!/потрачен|расход/i.test(out)) throw new Error('нет человеческого объяснения пустоты: ' + out);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_each_source_shows_its_channels_in_the_order_given():
    proc = _run("""
const out = usageMarkup({
  period: '2026-09', total: 470, limit: 1000, outside_sources: 0,
  sources: [{
    public_id: 'src-1', title: 'Вакансии AI', tokens: 470, summary_tokens: 70,
    channels: [
      { chat_id: -2, chat_title: 'beta', tokens: 300 },
      { chat_id: -1, chat_title: 'alpha', tokens: 100 },
    ],
  }],
});
if (!out.includes('Вакансии AI')) throw new Error('нет имени источника: ' + out);
if (out.indexOf('beta') > out.indexOf('alpha')) {
  throw new Error('каналы переставлены: самый дорогой обязан быть первым');
}
if (!out.includes('300') || !out.includes('100')) throw new Error('нет чисел расхода: ' + out);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_spend_outside_any_source_is_shown_too():
    """Иначе сумма на экране не сходится с общим числом, и веры ей нет."""
    proc = _run("""
const out = usageMarkup({
  period: '2026-09', total: 25, limit: 1000, outside_sources: 25, sources: [],
});
if (!out.includes('25')) throw new Error('расход вне источников потерян: ' + out);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_a_channel_name_from_telegram_cannot_inject_markup():
    """Имя канала пишет посторонний человек — это недоверенные данные.

    Оно приезжает из Telegram и хранится копией в строке расхода; путь
    «чужой текст → разметка» здесь такой же, как в ленте (задача 0.4).

    **Ошибка первой версии теста, исправлена в тесте.** Проверка живого
    атрибута была написана как `<\S+[^>]*onerror` — списана с `test_48`,
    где все теги идут с атрибутами (`<a href=...>`). Здесь теги голые
    (`<span>`), и `\S+` съедает закрывающую скобку: `<span>&lt;img src=x
    onerror` совпадает с образцом, хотя это правильно экранированный
    ТЕКСТ. Тест краснел на исправном коде. Образец переписан так, чтобы
    `[^>]*` не могла выйти за пределы своего тега.
    """
    proc = _run("""
const out = usageMarkup({
  period: '2026-09', total: 1, limit: 10, outside_sources: 0,
  sources: [{ public_id: 's', title: '<img src=x onerror=alert(1)>', tokens: 1, summary_tokens: 0,
    channels: [{ chat_id: -1, chat_title: '<script>alert(2)</script>', tokens: 1 }] }],
});
if (/<script/i.test(out)) throw new Error('живой script в разметке: ' + out);
if (/<[a-z][a-z0-9]*[^>]*\\son\\w+\\s*=/i.test(out)) throw new Error('живой обработчик в разметке: ' + out);
if (!out.includes('&lt;')) throw new Error('экранирования не видно вовсе: ' + out);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr
