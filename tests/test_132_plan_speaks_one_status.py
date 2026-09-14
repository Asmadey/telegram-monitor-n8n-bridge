"""Проза плана не спорит с его трекером (задача 13.4).

`CLAUDE.md` требует: «Статус живёт в одном месте (`docs/PLAN.md` + `PROGRESS.md`).
Остальные документы на него ссылаются, а не повторяют». Внутри самого плана
правило не соблюдалось: раздел 14 описывает фазы 8–10 и по ходу **выносит
вердикты** — «8.3 — 🟡 в работе», «9.4 — 🟡», «10.3 — не начато», — а трекер
раздела 15 отмечает те же задачи сделанными.

Дубль состояния не бывает долговечным: обновляют трекер, а проза остаётся. К
2026-09-14 разошлись четыре места, и всё это время план врал тому, кто читает
его первым по собственному протоколу. Тот же класс, что 12.1 (точка входа
описывала удалённый монолит), 12.4 (конфигурация сторожила несуществующий файл)
и 12.5 (карта документов молчала о живом источнике дизайн-системы). Разница
одна: здесь врёт документ, с которого начинается каждая сессия.

**Правило, которое здесь закрепляется:** проза вправе описывать задачу и её
замысел, но **вердикт о состоянии выносит только трекер**. Проза, назвавшая
состояние, обязана с трекером совпадать.

Запрещать вердикты в прозе полностью было бы проще, но неверно: фразы вроде
«8.1 — ✅ установлена» несут смысл абзаца, а не дублируют статус ради статуса.
Ловить надо расхождение, а не упоминание.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "PLAN.md"

_TRACKER = re.compile(r"^- \[([ x])\]\s+\*\*(\d+\.\d+[a-zA-Z]?)\*\*")
# «**9.4 Логи и границы тенантов — 🟡.**» и подобное
_VERDICT = re.compile(
    r"\*\*(\d+\.\d+[a-zA-Z]?)[.\s][^*]{0,80}?—\s*"
    r"(✅|🟡|❌|не начато|не выполнено|частично|не готово)"
)
_DONE = "✅"


def tracker_of(text: str) -> dict[str, bool]:
    found: dict[str, bool] = {}
    for line in text.splitlines():
        match = _TRACKER.match(line.strip())
        if match:
            found[match.group(2)] = match.group(1) == "x"
    return found


def prose_verdicts(text: str) -> list[tuple[int, str, str]]:
    """(строка, задача, вердикт) для утверждений вне таблиц и трекеров."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("|", "- [")):
            continue
        for match in _VERDICT.finditer(line):
            found.append((number, match.group(1), match.group(2)))
    return found


def contradictions(text: str) -> list[str]:
    tracker = tracker_of(text)
    bad = []
    for number, task, verdict in prose_verdicts(text):
        if tracker.get(task) is True and verdict != _DONE:
            bad.append(f"строка {number}: {task} — проза «{verdict}», трекер «сделано»")
    return bad


# --------------------------------------------------------------------------
# Self-test'ы сканера
# --------------------------------------------------------------------------


def test_the_scanner_sees_a_prose_verdict_that_contradicts_the_tracker():
    text = "**9.4 Логи — 🟡.** Ещё не всё.\n\n- [x] **9.4** Логи и границы тенантов\n"
    assert contradictions(text) == ["строка 1: 9.4 — проза «🟡», трекер «сделано»"], (
        contradictions(text)
    )


def test_the_scanner_accepts_prose_that_agrees_with_the_tracker():
    text = "**9.4 Логи — ✅ сделано.**\n\n- [x] **9.4** Логи и границы тенантов\n"
    assert contradictions(text) == [], "совпадающая проза принята за расхождение"


def test_the_scanner_ignores_the_tracker_itself():
    """В трекере «- [ ]» — это и есть статус, а не спор с ним."""
    text = "- [ ] **10.1** Живой сценарий — не начато\n- [x] **10.1** дубль\n"
    assert contradictions(text) == []


def test_the_scanner_is_not_fooled_by_an_unrelated_dash():
    text = "**9.4 Логи и границы — про тенантов.**\n\n- [x] **9.4** Логи\n"
    assert contradictions(text) == [], "обычное тире принято за вердикт"


# --------------------------------------------------------------------------
# Живой свип
# --------------------------------------------------------------------------


def test_the_sweep_actually_reads_the_plan():
    """Антивакуум: пустой разбор объявил бы план безупречным."""
    text = PLAN.read_text(encoding="utf-8")
    assert len(tracker_of(text)) >= 20, "трекеров разобрано подозрительно мало"
    assert len(prose_verdicts(text)) >= 5, "вердиктов в прозе разобрано мало"


def test_the_plan_prose_does_not_contradict_its_tracker():
    found = contradictions(PLAN.read_text(encoding="utf-8"))
    assert not found, (
        "проза плана спорит с его же трекером — читатель узнаёт состояние "
        "дважды и по-разному:\n  " + "\n  ".join(found)
    )
