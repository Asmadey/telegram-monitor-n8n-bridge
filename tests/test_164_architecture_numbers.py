"""Цифры в «Архитектуре» `CLAUDE.md` сверяются с деревом.

Зачем тест. Раздел «Текущее состояние» держал четыре числа прозой, и за месяц
все четыре разошлись с кодом: `17 таблиц` при 18, `14 ревизий, head 0014` при
19, `752 строки` при 835, `11 модулей` при 13. Расхождение по head было
опасным отдельно от остальных: в двух экранах ниже лежит «Крайне важный
факт» №2 про expand/contract, и агент, поверивший `head 0014`, рассуждал бы о
схеме пятью ревизиями назад — то есть принимал бы решения о миграциях по
несуществующему состоянию.

Нашлось это дважды независимо: владелец поправил цифры на `main` 2026-10-14,
агент — на своей ветке 2026-10-16. Две правки одного дрейфа столкнулись
конфликтом слияния. А пока PR висел, `main` уехал на `0020`, и `index.html`
дорос до 859 строк — то есть дрейф продолжался ровно в те часы, когда его
чинили.

Отсюда правило: **число в `CLAUDE.md`, которое можно посчитать по дереву,
обязано совпадать с деревом.** Проза этого не держит — это тот же вывод, что
закрыт `test_129` для карты документов и `test_123` для конфигурации.

Почему размер разметки проверяется полосой, а не равенством. `index.html`
меняется от любой правки вёрстки, и точное равенство заставляло бы править
`CLAUDE.md` в каждом таком коммите. Тест, который падает на шуме, рано или
поздно ослабят — а ослабленный тест хуже отсутствующего, потому что создаёт
видимость охраны. Поэтому здесь полоса ±10%, и `CLAUDE.md` пишет «~860»: сам
документ объявляет это оценкой, как и соседние «~3500 строк».
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _claimed(pattern: str) -> int:
    """Число, которое `CLAUDE.md` обещает по данному шаблону."""
    match = re.search(pattern, CLAUDE)
    assert match, f"в CLAUDE.md не нашлось утверждение по шаблону {pattern!r}"
    return int(match.group(1))


def test_claude_md_knows_the_real_migration_head() -> None:
    """Head-ревизия и их число — точно, без полос.

    Это та цифра, из-за которой тест и появился: ошибка в ней меняет
    рассуждение о схеме, а не только портит документ.
    """
    revisions = sorted(p.name for p in (ROOT / "alembic" / "versions").glob("*.py"))
    assert revisions, "не нашлось ни одной ревизии alembic"
    head = revisions[-1][:4]

    claimed_head = re.search(r"head `(\d+)`", CLAUDE)
    assert claimed_head, "в CLAUDE.md не нашлось утверждение про head-ревизию"
    assert claimed_head.group(1) == head, (
        f"CLAUDE.md обещает head `{claimed_head.group(1)}`, в дереве — `{head}`. "
        "Рядом лежит правило про expand/contract: по устаревшему head будут "
        "приняты решения о миграциях."
    )
    claimed_count = _claimed(r"\((\d+) ревизий")
    assert claimed_count == len(revisions), (
        f"CLAUDE.md обещает {claimed_count} ревизий, в дереве — {len(revisions)}"
    )


def test_claude_md_knows_how_many_tables_there_are() -> None:
    """Число таблиц — точно: оно меняется только миграцией."""
    models = (ROOT / "app" / "models" / "__init__.py").read_text(encoding="utf-8")
    tables = len(re.findall(r"^\s*__tablename__", models, re.MULTILINE))
    assert tables, "не нашлось ни одного __tablename__"
    claimed = _claimed(r"\*\*(\d+) таблиц")
    assert claimed == tables, (
        f"CLAUDE.md обещает {claimed} таблиц, в app/models/__init__.py — {tables}"
    )


def test_claude_md_knows_how_many_frontend_modules_there_are() -> None:
    """Число модулей `static/js/` — точно: файл добавляют осознанно."""
    modules = sorted(p.name for p in (ROOT / "static" / "js").glob("*.js"))
    assert modules, "не нашлось ни одного модуля static/js"
    claimed = _claimed(r"\*\*(\d+) модул\w+\*\* `static/js/`")
    listed = ", ".join(modules)
    assert claimed == len(modules), (
        f"CLAUDE.md обещает {claimed} модулей, в static/js/ — {len(modules)}: {listed}"
    )


def test_claude_md_markup_size_is_in_the_right_ballpark() -> None:
    """Размер разметки — полосой ±10%, см. причину в докстринге модуля."""
    lines = len(
        (ROOT / "static" / "index.html").read_text(encoding="utf-8").splitlines()
    )
    claimed = _claimed(r"`index\.html` — ~?(\d+) строк")
    assert abs(lines - claimed) <= lines * 0.10, (
        f"CLAUDE.md обещает ~{claimed} строк разметки, в index.html — {lines}. "
        "Разошлось больше чем на 10%: обновите число в «Архитектуре»."
    )
