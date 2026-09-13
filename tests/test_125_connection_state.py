"""Отказ сети и 5xx дают видимое состояние, а не тишину (задача 12.8).

Перехват в `api.js` был ровно один — на 401, и он появился не от хорошей
жизни: 7 сентября на живом деплое истёкшая сессия дала стену тостов, по одному
от каждого модуля. Вывод тогда сделали правильный — «перехват ОДИН на все
вызовы», — но применили его к одному классу отказов из трёх.

Остальные два остались без хозяина:

- **5xx.** Каждый модуль показывает свой тост, а `feed.js` на ошибке загрузки
  пишет в `console.error` и молчит. Пустая лента при лежащем сервере выглядит
  ровно как пустая лента — «ничего не найдено» и «не смогли посмотреть»
  неразличимы. Это тот же класс отказа, который ловили всю фазу 9.
- **Оборванная сеть.** `fetch` бросает, и сообщение, которое видит человек, —
  браузерное `Failed to fetch` (в Safari `Load failed`). По-английски, без
  подсказки, что делать.

Лечится это не тостом: тост живёт три секунды, а сервер лежит минутами. Нужно
состояние, которое **держится**, пока не пройдёт успешный запрос, — и одно на
всё приложение, иначе вернётся стена сообщений.

Проверка поведенческая: node импортирует `api.js` с подставными `fetch` и
`document` и прогоняет четыре исхода — 200, 500, обрыв сети, снова 200. Regex
здесь не годится: вопрос не «есть ли в файле нужные слова», а «что увидит
человек после такого-то ответа».
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Подставное окружение браузера: api.js обращается к document и window,
# node их не имеет. Стабы ставятся ДО импорта модуля.
HARNESS = """
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        remove(c) { this._set.delete(c); },
        contains(c) { return this._set.has(c); },
      },
    };
  }
  return elements[id];
}
element('connectionBanner');
element('connectionBannerText');

globalThis.document = {
  cookie: '',
  getElementById: (id) => elements[id] || null,
};
let redirectedTo = null;
globalThis.window = {
  location: { pathname: '/', search: '', replace: (u) => { redirectedTo = u; } },
};

let nextOutcome = null;
globalThis.fetch = async () => {
  if (typeof nextOutcome === 'function') return nextOutcome();
  return nextOutcome;
};

const { apiGet } = await import('./static/js/api.js');
const banner = elements['connectionBanner'];
const shown = () => banner.classList.contains('open');
"""


def _run(body: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение api.js не исполнить")
    return subprocess.run(
        [node, "--input-type=module", "--eval", HARNESS + body],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_a_good_answer_shows_nothing():
    """Антивакуум: состояние, висящее всегда, ничего не сообщает."""
    proc = _run("""
nextOutcome = new Response('{}', { status: 200 });
await apiGet('/api/feed');
if (shown()) throw new Error('состояние отказа висит после успешного ответа');
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_a_server_error_is_visible():
    proc = _run("""
nextOutcome = new Response('boom', { status: 500 });
await apiGet('/api/feed');
if (!shown()) throw new Error('после 500 человек не видит ничего: пустой экран при лежащем сервере неотличим от пустого экрана');
const text = elements['connectionBannerText'].textContent;
if (!/сервер/i.test(text)) throw new Error('сообщение не называет причину: ' + text);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_a_broken_network_is_visible_and_speaks_russian():
    proc = _run("""
nextOutcome = () => { throw new TypeError('Failed to fetch'); };
let caught = null;
try { await apiGet('/api/feed'); } catch (e) { caught = e; }
if (!shown()) throw new Error('обрыв сети не виден на экране');
if (!caught) throw new Error('ошибка проглочена: вызывающий код не узнает об отказе');
if (/failed to fetch|load failed/i.test(caught.message)) {
  throw new Error('человек видит браузерное сообщение по-английски: ' + caught.message);
}
if (!/[а-яё]/i.test(caught.message)) throw new Error('сообщение не на русском: ' + caught.message);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_the_state_clears_when_the_server_answers_again():
    """Состояние, которое не снимается, через минуту врёт так же, как тишина."""
    proc = _run("""
nextOutcome = new Response('boom', { status: 500 });
await apiGet('/api/feed');
if (!shown()) throw new Error('после 500 состояние не показано');
nextOutcome = new Response('{}', { status: 200 });
await apiGet('/api/feed');
if (shown()) throw new Error('состояние отказа осталось после успешного ответа');
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_the_401_interceptor_still_works():
    """Регрессия: перехват входа старше этой задачи и переживать её обязан."""
    proc = _run("""
nextOutcome = new Response('', { status: 401 });
await apiGet('/api/feed');
if (redirectedTo === null) throw new Error('401 больше не уводит на вход');
if (!String(redirectedTo).includes('/login')) throw new Error('увело не туда: ' + redirectedTo);
console.log('OK');
""")
    assert proc.returncode == 0, proc.stderr


def test_the_banner_exists_in_the_markup():
    """Состояние без элемента в разметке — это `null` и молчание.

    Правило показа проверяет `test_118` (id, которому модуль переключает класс
    показа, обязан иметь правило в таблице стилей), поэтому здесь только
    наличие самого элемента.
    """
    markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="connectionBanner"' in markup, "в разметке нет полосы состояния связи"
    assert 'id="connectionBannerText"' in markup, (
        "полосе состояния нечем сказать причину"
    )


def test_the_banner_has_a_rule_in_the_stylesheet():
    """Правило показа — здесь, а не в `test_118`, и на то есть причина.

    `test_118` связывает id с правилом по образцу
    `const x = document.getElementById(...)` + `x.classList.add('open')`.
    Полоса состояния так не объявляется: элемент ищется при показе, а не при
    импорте (см. тест ниже), и связка распадается. Свип, который не видит
    элемент, промолчал бы — поэтому стык проверяется явно.
    """
    css = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
    assert "#connectionBanner {" in css, (
        "у полосы состояния нет правила: без него она видна всегда"
    )
    assert "#connectionBanner.open" in css, (
        "нет правила показа — класс open переключается впустую"
    )


def test_the_api_module_imports_without_a_dom():
    """`api.js` обязан импортироваться без браузера.

    Регрессия, пойманная при этой же задаче: поиск элемента на верхнем уровне
    модуля уронил `test_102` — он гоняет `secrets.js` в node, а тот тянет
    `api.js`. Поведенческая проверка секретов (что именно уходит на сервер из
    формы с маской) держится на этом свойстве, и терять его нельзя.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен")
    proc = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            "await import('./static/js/api.js'); console.log('OK');",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, (
        f"api.js не импортируется без DOM — ломает поведенческие тесты в node:\n{proc.stderr}"
    )
