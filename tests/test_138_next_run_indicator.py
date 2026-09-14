"""Карточка источника снова говорит, когда будет следующий прогон.

**Находка владельца:** «куда делись индикаторы — во сколько был последний
прогон и сколько осталось до следующего?»

Один из двух на месте: «Прогон: 17:37» — это и есть время последнего. Второй
исчез целиком, и исчез в три приёма, все три в одном коммите 11.7
(`channels.js` → `sources.js`, 2026-09-10):

1. из разметки карточки пропал сегмент «След: …» вместе с пульсирующей точкой;
2. из `main.js` пропал вызов `refreshMonitorTimers()` — **сам `setInterval`
   остался**, и с тех пор каждые 15 секунд исправно исполняет пустое тело.
   Комментарий над ним по-прежнему обещает «периодический пересчёт таймеров
   обратного отсчёта»;
3. осиротевший `formatNextRun` позже убрал свип мёртвого кода 12.3 — по своему
   правилу верно: его никто не звал. Свип не умеет отличать «убрано намеренно»
   от «потеряно при переезде», и в этом его предел, а не ошибка.

Пережили переезд только правила CSS: `.timeline-next-status` и `.pulse-dot`
лежат в `main.css` до сих пор. Это и есть отпечаток потери — **стиль пережил
разметку**, третий случай того же класса после «текст пережил код» (12.1,
12.4, 12.5, 13.4). Поэтому здесь не только возвращается индикатор, но и
заводится свип, который такую потерю называет.

Расчёт проверяется НАСТОЯЩИМ исполнением в node: «через сколько» на регулярках
не проверяется, а именно оно и есть ответ на вопрос владельца.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"
SOURCES_JS = (JS_DIR / "sources.js").read_text(encoding="utf-8")
MAIN_JS = (JS_DIR / "main.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")


def _node(body: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — расчёт не исполнить")
    return subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            "import { formatNextRun } from './static/js/render.js';\n" + body,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _says(source_js: str, *, minutes_ago: float, interval: int, active=True) -> str:
    """Что покажет карточка источнику, который прогонялся `minutes_ago` назад."""
    result = _node(
        "const now = new Date('2026-09-14T12:00:00Z');\n"
        f"const last = new Date(now.getTime() - {minutes_ago} * 60000);\n"
        "process.stdout.write(formatNextRun({"
        f"  is_active: {str(active).lower()},"
        f"  interval_minutes: {interval},"
        "  last_run_at: last.toISOString()"
        "}, now));"
    )
    assert result.returncode == 0, f"расчёт не исполнился: {result.stderr}"
    return result.stdout


# --------------------------------------------------------------------------
# Индикатор на месте и живой
# --------------------------------------------------------------------------


def test_the_card_shows_when_the_next_run_is():
    """Сегмент вернулся в разметку карточки — вместе со своим стилем."""
    assert "formatNextRun" in SOURCES_JS, "карточка не считает время следующего прогона"
    for mark in ("timeline-next-status", "pulse-dot"):
        assert mark in SOURCES_JS, (
            f"разметка не пользуется классом {mark} — правило в main.css лежит "
            "зря, а индикатора на карточке нет"
        )


def test_the_countdown_is_recomputed_and_not_just_promised():
    """Пустой `setInterval` — главный след этой потери.

    Таймер каждые 15 секунд исполняет пустое тело, а комментарий над ним
    обещает пересчёт. Проверяется тело, а не комментарий.
    """
    empty = re.findall(r"set(?:Interval|Timeout)\(\s*\(\s*\)\s*=>\s*\{\s*\}", MAIN_JS)
    assert not empty, (
        f"в main.js {len(empty)} таймер(ов) с пустым телом: обещание в "
        "комментарии исполняется каждые несколько секунд и не делает ничего"
    )
    tail = (
        MAIN_JS[MAIN_JS.index("обратного отсчёта") :]
        if ("обратного отсчёта" in MAIN_JS)
        else MAIN_JS[MAIN_JS.index("обратного отсчета") :]
    )
    assert "refreshSourceTimers" in tail[:300], (
        "таймер обратного отсчёта ничего не пересчитывает — счётчик замрёт "
        "на том, что успело нарисоваться при загрузке"
    )
    assert "export function refreshSourceTimers" in SOURCES_JS, (
        "пересчёт некому сделать: модуль источников такой функции не отдаёт"
    )


def test_the_recount_does_not_redraw_the_whole_list():
    """Пересчёт трогает только текст счётчика.

    Прежняя версия перерисовывала список целиком. Тогда это было безобидно;
    теперь кнопка «Запустить» гаснет на время запроса к серверу (11.7), и
    перерисовка раз в 15 секунд вернула бы её в рабочее состояние посреди
    запроса — то есть позволила бы запустить прогон дважды.
    """
    body = SOURCES_JS[SOURCES_JS.index("export function refreshSourceTimers") :][:600]
    assert "renderSources" not in body, (
        "пересчёт счётчика перерисовывает весь список — погасшая кнопка "
        "«Запустить» вернётся в строй посреди запроса"
    )
    assert "data-next-run" in SOURCES_JS, "счётчику нечем адресоваться в разметке"


# --------------------------------------------------------------------------
# Сам расчёт — исполнением, а не разбором
# --------------------------------------------------------------------------


def test_a_paused_source_does_not_count_down():
    assert "паузе" in _says(SOURCES_JS, minutes_ago=10, interval=60, active=False), (
        "приостановленный источник показывает отсчёт до прогона, которого не будет"
    )


def test_an_overdue_source_says_the_run_is_due_now():
    """Прогон просрочен: воркер возьмёт источник ближайшим тиком."""
    assert "сейчас" in _says(SOURCES_JS, minutes_ago=90, interval=60), (
        "просроченный источник показывает отсчёт в прошлое"
    )


def test_less_than_an_hour_is_counted_in_minutes():
    text = _says(SOURCES_JS, minutes_ago=18, interval=60)
    assert "42 мин" in text, f"не сказано, сколько осталось: {text!r}"


def test_more_than_an_hour_is_counted_in_hours():
    text = _says(SOURCES_JS, minutes_ago=85, interval=1440)
    # 1440 − 85 = 1355 минут = 22 ч 35 мин
    assert "22 ч" in text and "35 мин" in text, (
        f"сутки до прогона показаны как {text!r} — в минутах это не читается"
    )


def test_a_source_that_never_ran_is_due_now():
    """Часов у него нет, и ждать нечего: он попадёт в ближайший тик."""
    result = _node(
        "process.stdout.write(formatNextRun("
        "{is_active: true, interval_minutes: 60, last_run_at: null}, new Date()));"
    )
    assert result.returncode == 0, result.stderr
    assert "сейчас" in result.stdout, (
        f"источник без прогонов показывает {result.stdout!r} вместо срока"
    )


def test_the_counter_is_text_and_not_markup():
    """Прежняя версия отдавала HTML и вставлялась через `raw()`.

    Всё, что идёт через `raw()`, обязано быть доверенным навсегда — включая
    правки, которых ещё нет. Экранирование по умолчанию (5.2) держится
    ровно на том, что таких мест мало.
    """
    result = _node(
        "process.stdout.write(formatNextRun("
        "{is_active: false, interval_minutes: 60, last_run_at: null}, new Date()));"
    )
    assert result.returncode == 0, result.stderr
    assert "<" not in result.stdout, f"расчёт вернул разметку: {result.stdout!r}"


def test_the_last_run_is_not_mistaken_for_the_next():
    """Два времени в одной строке обязаны различаться подписями.

    Пока сосед был на месте, «Прогон: 17:37» читалось однозначно. В одиночку
    оно с равным успехом читается как время следующего.
    """
    row = SOURCES_JS[SOURCES_JS.index("meta-timeline-row") :][:1200]
    assert "Последний" in row, (
        "время последнего прогона подписано так, что его не отличить от "
        "времени следующего"
    )


# --------------------------------------------------------------------------
# Свип: стиль не переживает разметку
# --------------------------------------------------------------------------

# Правило в CSS без разметки, которая его носит, — отпечаток потерянной при
# переезде части интерфейса. Здесь названо всё, что осиротело раньше: у
# каждого случая своя причина, и ни одна из них не «неизвестно зачем».
KNOWN_ORPHANS = {
    "clean-pill-accent": (
        "чип «Отправлено: N • ID X» на карточке канала, снесён переездом 11.7. "
        "Сервер по-прежнему считает sent_count и отдаёт его в /api/sources — "
        "решение владельца, возвращать ли чип"
    ),
    "clean-pill-purple": (
        "чип «LLM Промпт», снесён переездом 11.7: промпт извлечения теперь у "
        "каждого канала свой и правится в модалке"
    ),
    "dialog-item": (
        "строка списка диалогов до 11.7; «Из диалогов» работает и сегодня, но "
        "список рисуется другой разметкой"
    ),
    "quick-model-tag": (
        "чипы-подсказки моделей, убраны намеренно вместе с фильтром (3b5ebc7)"
    ),
    "stat-card": "плитка сводки главного экрана, снята разрезом 5.1",
    "stat-value": "цифра в плитке сводки, снята разрезом 5.1",
    "stats-grid": "сетка плиток сводки, снята разрезом 5.1",
}


def _class_selectors(css: str) -> set[str]:
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", css))


def _markup_and_scripts() -> str:
    parts = [p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js"))]
    parts += [
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "static").glob("*.html"))
    ]
    return "\n".join(parts)


def test_the_selector_parser_sees_what_it_should():
    """Self-test разборщика: свип на регулярке обязан доказать, что читает.

    Без этого пустой разбор выглядит как «сирот нет» — и свип зеленеет тем
    громче, чем хуже работает.
    """
    sample = ".alpha { color: red; }\n#beta .gamma-two:hover { color: blue; }\n"
    found = _class_selectors(sample)
    assert {"alpha", "gamma-two"} <= found, f"разборщик не видит классы: {found}"
    assert "beta" not in found, "разборщик принял id за класс"


def test_no_style_outlives_the_markup_that_wore_it():
    classes = _class_selectors(CSS)
    # Страж пустоты: разборщик, вернувший ничего, обязан падать здесь, а не
    # молча объявлять, что сирот нет.
    assert len(classes) > 50, f"разобрано {len(classes)} классов — это не вся таблица"

    used = _markup_and_scripts()
    orphans = sorted(c for c in classes if c not in used and c not in KNOWN_ORPHANS)
    assert not orphans, (
        f"правила без разметки: {orphans}. Стиль, переживший разметку, — "
        "отпечаток части интерфейса, потерянной при переезде (так пропал "
        "счётчик до следующего прогона). Либо верните разметку, либо удалите "
        "правило, либо назовите причину в KNOWN_ORPHANS"
    )


def test_the_registry_does_not_outlive_its_own_entries():
    """Обратная сторона: имя в списке, которого нет в CSS, — снова мусор."""
    classes = _class_selectors(CSS)
    stale = sorted(name for name in KNOWN_ORPHANS if name not in classes)
    assert not stale, f"в списке исключений имена, которых в main.css уже нет: {stale}"
