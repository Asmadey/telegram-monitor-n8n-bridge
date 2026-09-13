"""Конфигурация не сторожит того, чего нет, и не выдаёт снятое за живое
(задача 12.4 `docs/PLAN.md`).

Монолит `server.py` снят задачей 7.4 и запрещён к возвращению (`test_75`).
Конфигурация об этом не узнала: `ruff.toml` исключает его из линта, `Dockerfile`
и `Procfile` объясняют в настоящем времени, почему его нельзя запускать,
`.dockerignore` держит правило.

Само по себе это ничего не ломает — и ровно поэтому опасно. Именно по таким
следам читатель заключает, что монолит на месте: по ним его заключил и
`CLAUDE.md`, полторы фазы описывавший удалённый файл как «весь бэкенд»
(задача 12.1). Конфигурация читается чаще документации и выглядит убедительнее:
раз линтер обходит файл стороной, файл, надо полагать, есть.

Отсюда два разных правила, и они не совпадают:

1. **Исключение из линта обязано называть существующий файл.** Исключение —
   это разрешение не проверять. Разрешение, выданное пустому месту, — мусор,
   который переживёт любую уборку, потому что никогда не сработает и не
   пожалуется.

2. **Правило `.dockerignore` остаётся, а комментарий обязан говорить правду.**
   Здесь снимать страховку не за что: `COPY . .` заберёт всё, что окажется в
   контексте, и стоит правило одну строку. Врать оно перестаёт не удалением, а
   прошедшим временем: «снят задачей 7.4, правило — заслон на случай возврата».

Второе правило проверяется так же, как `test_120` проверяет `CLAUDE.md`:
упоминание допустимо, если рядом стоит пометка, что монолита больше нет.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Файлы, которые читает машина, а не только человек. Журналы (`PROGRESS.md`,
# `docs/PLAN.md`) сюда не входят намеренно: запись о прошлом обязана остаться
# в прошедшем времени и без правок.
CONFIG_FILES = ("ruff.toml", "Dockerfile", "Procfile", ".dockerignore", "railway.json")

MONOLITH = re.compile(r"server\.py|server:app")

# Пометки, по которым видно, что речь о снятом, а не о живом.
HISTORY_MARKERS = ("снят", "удал", "7.4", "больше нет", "не существует", "возврат")

WINDOW = 2


def _monolith_mentions_without_history(text: str) -> list[str]:
    lines = text.splitlines()
    guilty = []
    for i, line in enumerate(lines):
        if not MONOLITH.search(line):
            continue
        window = " ".join(lines[max(0, i - WINDOW) : i + WINDOW + 1]).lower()
        if not any(marker in window for marker in HISTORY_MARKERS):
            guilty.append(line.strip())
    return guilty


# --------------------------------------------------------------------------
# Self-test'ы сканера: обязан краснеть и обязан не краснеть
# --------------------------------------------------------------------------


def test_scanner_flags_a_mention_that_sounds_alive():
    text = "# Монолит server.py из образа не запускается никогда\nserver.py\n"
    assert _monolith_mentions_without_history(text), (
        "сканер не увидел упоминание монолита в настоящем времени"
    )


def test_scanner_accepts_a_mention_marked_as_history():
    text = (
        "# Монолит снят задачей 7.4; правило — заслон на случай возврата\nserver.py\n"
    )
    assert not _monolith_mentions_without_history(text), (
        "сканер требует убрать упоминание, честно помеченное как история"
    )


def test_scanner_reads_the_window_and_not_just_the_line():
    """Пометка обычно стоит строкой выше правила, а не на ней самой."""
    text = "# снят задачей 7.4\n#\n# заслон на случай возврата\nserver.py\n"
    assert not _monolith_mentions_without_history(text), (
        "окно не просматривается — пометка выше правила не засчитана"
    )


# --------------------------------------------------------------------------
# Правило 1: исключение из линта обязано называть существующий файл
# --------------------------------------------------------------------------


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def _ruff_literal_exclusions() -> list[str]:
    src = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    block = re.search(r"extend-exclude\s*=\s*\[(.*?)\]", src, re.S)
    assert block, "в ruff.toml не найден extend-exclude — сканер ослеп"
    return [
        item
        for item in re.findall(r'"([^"]+)"', block.group(1))
        if "*" not in item and "?" not in item
    ]


def test_lint_exclusion_list_is_not_empty():
    """Антивакуум: пустой разбор списка зеленеет сам по себе."""
    literals = _ruff_literal_exclusions()
    assert len(literals) >= 3, f"буквальных исключений разобрано {len(literals)} — мало"


def test_every_lint_exclusion_names_a_file_that_exists():
    tracked = _tracked_files()
    assert len(tracked) > 100, "git ls-files пуст — проверка вырождена"
    missing = [name for name in _ruff_literal_exclusions() if name not in tracked]
    assert not missing, (
        "линтер получил разрешение не проверять то, чего нет "
        f"(разрешение переживёт любую уборку и не пожалуется): {missing}"
    )


# --------------------------------------------------------------------------
# Правило 2: конфигурация не выдаёт снятое за живое
# --------------------------------------------------------------------------


def test_config_files_do_not_describe_the_monolith_as_alive():
    guilty: dict[str, list[str]] = {}
    seen = 0
    for name in CONFIG_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        seen += 1
        found = _monolith_mentions_without_history(path.read_text(encoding="utf-8"))
        if found:
            guilty[name] = found
    assert seen >= 4, f"конфигурационных файлов найдено {seen} — выборка пуста"
    assert not guilty, (
        "конфигурация говорит о монолите в настоящем времени, хотя он снят "
        f"задачей 7.4 (по таким следам читатель и заключает, что он на месте): {guilty}"
    )
