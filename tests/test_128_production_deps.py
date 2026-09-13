"""Боевые зависимости обслуживают боевые процессы (часть задачи 12.2).

В образ ставится `requirements.txt`. Из него поднимаются ровно два процесса
(`Procfile`): `web` — `uvicorn app.main:app`, `worker` — `python -m app.worker`,
плюс `alembic upgrade head` перед стартом. Всё, что нужно только скриптам
разработчика, в этом списке лишнее: оно ставится на каждый деплой, увеличивает
образ и — важнее — расширяет поверхность, которую приходится держать в голове
при разборе уязвимостей (`pip-audit` идёт именно по `requirements.txt`).

Найдено ревью 2026-09-10: `rich` (2.9 МБ) и `tabulate` попали в боевой список
ради четырёх дореформенных CLI-инструментов в корне. `tabulate` при этом не
импортирует ВООБЩЕ НИКТО — ни скрипты, ни приложение: пакет ставился в каждый
образ, чтобы обслуживать код, которого нет.

Судьба самих скриптов — решение владельца (задача 12.2), и этот тест её не
предвосхищает: он говорит не «скриптов быть не должно», а «боевой список — про
`app/` и `alembic/`». Инструменты разработчика живут в `requirements-dev.txt`,
который тянет боевой список целиком, поэтому запускать их по-прежнему можно.

Пакеты, которые нужны без единой строки `import`, перечислены ниже с
объяснением. Список — не поблажка, а место, где такое объяснение обязано
существовать: незаписанная причина через полгода неотличима от забытого хвоста.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Код, который исполняют боевые процессы. `scripts/` включён намеренно:
# перенос старой базы (`python -m scripts.migrate_sqlite_to_pg`, задача 10.2b)
# описан в `CLAUDE.md` как боевая операция и выполняется на том же образе —
# сузить список до `app/` значило бы однажды снести зависимость у него из-под ног.
SHIPPED = ("app", "alembic", "scripts")

# имя пакета → имя модуля, если они расходятся
MODULE_OF = {
    "python-dotenv": "dotenv",
    "sqlalchemy[asyncio]": "sqlalchemy",
    "pydantic-settings": "pydantic_settings",
    "email-validator": "email_validator",
    "google-auth[requests]": "google.auth",
}

# Нужны боевому образу, но ни одной строкой `import` не видны.
USED_WITHOUT_IMPORT = {
    "uvicorn": "команда запуска web-процесса (Procfile, CMD Dockerfile)",
    "asyncpg": "драйвер SQLAlchemy: подключается по схеме URL postgresql+asyncpg",
    "aiosqlite": "драйвер SQLite для asyncio: живой Postgres есть не везде",
    "python-dotenv": "pydantic-settings читает им env_file (app/config.py)",
    "email-validator": "проверка адреса в pydantic EmailStr (signup/login, 2.4)",
}


def _requirements() -> list[str]:
    names = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(re.split(r"[<>=!~]", line)[0].strip())
    return names


def _modules_imported_by_shipped_code() -> set[str]:
    found: set[str] = set()
    for folder in SHIPPED:
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module.split(".")[0])
    return found


def test_the_scan_sees_the_application():
    """Антивакуум: пустой разбор объявил бы лишним весь список."""
    imported = _modules_imported_by_shipped_code()
    assert len(imported) >= 20, f"импортов найдено {len(imported)} — сканер ослеп"
    assert {"fastapi", "sqlalchemy", "telethon"} <= imported, (
        f"сканер не видит очевидных боевых зависимостей: {sorted(imported)[:20]}"
    )
    assert len(_requirements()) >= 10, "разбор requirements.txt дал слишком мало"


def test_every_production_dependency_serves_the_production_processes():
    imported = _modules_imported_by_shipped_code()
    stray = []
    for name in _requirements():
        if name in USED_WITHOUT_IMPORT:
            continue
        module = MODULE_OF.get(name, name.replace("-", "_")).split(".")[0]
        if module not in imported:
            stray.append(name)
    assert not stray, (
        "в боевом списке пакеты, которых не касаются ни app/, ни alembic/: "
        f"{stray}. Инструментам разработчика место в requirements-dev.txt; "
        "если пакет нужен без import — впишите его в USED_WITHOUT_IMPORT с причиной"
    )


def test_the_exception_list_does_not_outlive_its_packages():
    """Объяснение для пакета, которого в списке уже нет, — такой же хвост.

    Ровно этим и была строка `server.py` в `ruff.toml` (задача 12.4): запись
    пережила то, что объясняла, и продолжала утверждать обратное.
    """
    names = set(_requirements())
    stale = sorted(set(USED_WITHOUT_IMPORT) - names)
    assert not stale, f"объяснение осталось без пакета: {stale}"


def test_developer_tools_stay_installable():
    """Перенос не должен ломать сами инструменты: dev тянет боевой список."""
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dev, (
        "requirements-dev.txt перестал тянуть боевой список — перенос сломает "
        "и сами инструменты, и CI"
    )
