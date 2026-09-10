"""Точка входа не врёт о дереве (найдено ревью 2026-09-10).

`CLAUDE.md` читается ПЕРВЫМ в каждой сессии — так велит его собственный
протокол. К моменту ревью он описывал архитектуру, которой не было полутора
фаз: «`server.py` (~2000 строк) — весь бэкенд», хотя монолит удалён задачей
7.4, а команда запуска `uvicorn server:app` просто падала. Агент, честно
выполнивший протокол, получал неверную картину мира и нерабочую команду.

Проверяется одно свойство, зато машинно: **путь, названный в точке входа,
обязан быть в репозитории.** Это ловит и удалённый файл, и переименование, и
переезд каталога.

Сверка идёт с `git ls-files`, а не с диском — и это не придирка. Первая версия
теста смотрела на файловую систему, брала пути из блока команд и падала в CI на
`['.venv/bin/alembic', '.venv/bin/pip', '.venv/bin/python']`: окружение в git не
хранится, в чекауте его нет. Ровно этот отказ записан в докстринге `test_03` —
«написаны под локальную раскладку и работали только на машине автора». Индекс
git одинаков и здесь, и в CI, поэтому сверять надо с ним.

Прозу — «что это за проект», «почему так решили» — тест не проверяет и не
может. Её обязан пересматривать человек; см. `PROGRESS.md` за 2026-09-10.
"""

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAUDE = ROOT / "CLAUDE.md"

# Путь в обратных кавычках: файл с известным расширением или каталог со слэшем.
_QUOTED = re.compile(r"`([^`\n]+)`")
_LOOKS_LIKE_PATH = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|md|json|toml|ini|txt|cfg|yml)|(?:[\w.-]+/)+"
)

# Строка рассказывает о прошлом, а не отправляет по адресу.
_HISTORY = re.compile(
    r"удал|снят|истори|раньше|прежн|тогда|было|переехал|апстрим|7\.4|до 2026-09-10",
    re.IGNORECASE,
)

# Окружение в git не хранится — сверять его с индексом бессмысленно.
_NOT_OURS = (".venv/", "http")


def _tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def _paths_named_in_the_entry_point() -> list[tuple[int, str]]:
    """(номер строки, путь) для каждого пути, поданного как действующий."""
    found: list[tuple[int, str]] = []
    lines = CLAUDE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        # Пометка времени может уехать на соседнюю строку — markdown переносит.
        window = " ".join(lines[max(0, index - 1) : index + 2])
        if _HISTORY.search(window):
            continue
        for token in _QUOTED.findall(line):
            token = token.strip()
            if token.startswith(_NOT_OURS):
                continue
            if _LOOKS_LIKE_PATH.fullmatch(token):
                found.append((index + 1, token))
    return found


def test_the_sweep_sees_something():
    """Защита от вакуумности: пустая выборка зеленела бы всегда.

    Порог не круглый, а измеренный: на 2026-09-10 точка входа называет
    одиннадцать действующих путей. Просядет вдвое — значит выборка сломалась,
    а не документ похудел.
    """
    paths = {p for _, p in _paths_named_in_the_entry_point()}
    assert len(paths) >= 8, f"путей под проверкой подозрительно мало: {sorted(paths)}"


def test_every_path_named_in_the_entry_point_is_in_the_repository():
    tracked = _tracked()
    missing = []
    for number, path in _paths_named_in_the_entry_point():
        ok = (
            any(f.startswith(path) for f in tracked)
            if path.endswith("/")
            else path in tracked
        )
        if not ok:
            missing.append(f"{number}: {path}")
    assert not missing, (
        "точка входа ссылается на то, чего в репозитории нет — читатель пойдёт "
        "по адресу и не найдёт файла:\n  " + "\n  ".join(missing)
    )


def test_the_retired_monolith_is_not_described_as_present():
    """`server.py` снят задачей 7.4. Упоминание допустимо только как история."""
    lines = CLAUDE.read_text(encoding="utf-8").splitlines()
    offenders = []
    for index, line in enumerate(lines):
        if "server.py" not in line and "server:app" not in line:
            continue
        window = " ".join(lines[max(0, index - 1) : index + 2])
        if _HISTORY.search(window):
            continue
        offenders.append(f"{index + 1}: {line.strip()[:100]}")
    assert not offenders, (
        "монолит описан как существующий — он удалён задачей 7.4:\n  "
        + "\n  ".join(offenders)
    )
