"""Точка входа не врёт о дереве (найдено ревью 2026-09-10).

`CLAUDE.md` читается ПЕРВЫМ в каждой сессии — так велит его собственный
протокол. К моменту ревью он описывал архитектуру, которой не было полутора
фаз: «`server.py` (~2000 строк) — весь бэкенд», хотя монолит удалён задачей
7.4, а команда запуска `uvicorn server:app` просто падала. Агент, честно
выполнивший протокол, получал неверную картину мира и нерабочую команду.

Документ не может проверить сам себя, а тесты проверяли что угодно, кроме
него. Здесь закрывается ровно одно свойство, зато машинно проверяемое:
**путь, названный в блоке команд, обязан существовать**. Это ловит и
удалённый файл, и переименование, и переезд каталога.

Прозу — «что это за проект», «почему так решили» — тест не проверяет и не
может. Её обязан пересматривать человек; см. `PROGRESS.md` за 2026-09-10.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAUDE = ROOT / "CLAUDE.md"

# Пути внутри блоков ```bash ... ``` — то, что читатель скопирует и запустит.
_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
# Файл или каталог проекта: со слэшем или с известным расширением.
_PATH = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|json|toml|ini|md|txt))"
)

# Не пути проекта: аргументы командной строки и чужие адреса.
_IGNORED_PREFIXES = ("http", "app.main", "app.worker", "127.0.0.1", "0.0.0.0")


def _paths_in_commands() -> set[str]:
    found: set[str] = set()
    for block in _FENCE.findall(CLAUDE.read_text(encoding="utf-8")):
        for line in block.splitlines():
            line = line.split("#", 1)[0]  # комментарий — не команда
            for hit in _PATH.findall(line):
                if hit.startswith(_IGNORED_PREFIXES):
                    continue
                found.add(hit)
    return found


def test_the_sweep_sees_something():
    """Защита от вакуумности: пустая выборка зеленела бы всегда."""
    paths = _paths_in_commands()
    assert len(paths) >= 5, f"в блоке команд подозрительно мало путей: {paths}"


def test_every_path_named_in_the_commands_exists():
    missing = sorted(p for p in _paths_in_commands() if not (ROOT / p).exists())
    assert not missing, (
        "точка входа велит запускать то, чего в дереве нет — команда упадёт у "
        "первого, кто её скопирует:\n  " + "\n  ".join(missing)
    )


def test_the_retired_monolith_is_not_described_as_present():
    """`server.py` снят задачей 7.4. Упоминание допустимо только как история."""
    # Смотреть надо предложение, а не строку: markdown переносит текст, и
    # первая версия теста краснела на фразе «Монолита больше нет — server.py
    # удалён задачей 7.4», потому что слово «удалён» уехало на строку ниже.
    lines = CLAUDE.read_text(encoding="utf-8").splitlines()
    offenders = []
    for index, line in enumerate(lines):
        if "server.py" not in line and "server:app" not in line:
            continue
        window = " ".join(lines[max(0, index - 1) : index + 2])
        if re.search(r"удал|снят|истори|раньше|было|прежн|7\.4", window, re.IGNORECASE):
            continue
        offenders.append(f"{index + 1}: {line.strip()[:100]}")
    assert not offenders, (
        "монолит описан как существующий — он удалён задачей 7.4:\n  "
        + "\n  ".join(offenders)
    )
