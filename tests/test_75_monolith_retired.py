"""Задача 7.4 — монолит в отставке.

`server.py` был рантаймом проекта: ~2000 строк, весь бэкенд, ~40
эндпоинтов **без единой проверки авторизации** (К2), SQLite, глобальный
синглтон Telethon, схема через `try: ALTER TABLE except: pass`. Код из
него перенесён в `app/` задачами Фаз 1–5, запускался он уже ниоткуда —
`Procfile`, `railway.json` и `Dockerfile` ведут на `app.main:app`, а
`.dockerignore` не пускает файл в образ.

Но пока файл лежит в дереве, он остаётся заряженным ружьём: `uvicorn
server:app` поднимает открытый интернету сервис с чужими MTProto-сессиями
одной командой, и никакой конфиг этого не запрещает — запрещает только
отсутствие файла. Поэтому проверка на удаление, а не на «не запускается».

Тест — трипваер навсегда: возвращение монолита (или ссылки на него в
документации, по которой кто-то его запустит) снова становится красным.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

DOCS = ("README.md", "PROJECT_OVERVIEW.md")


def test_monolith_file_is_gone():
    assert not (ROOT / "server.py").exists(), (
        "server.py всё ещё в дереве: `uvicorn server:app` поднимает ~40 "
        "эндпоинтов без авторизации над базой с чужими MTProto-сессиями"
    )


def test_no_module_imports_the_monolith():
    """Импорт `server` откуда угодно вернул бы монолит в рантайм."""
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "import server" in src or "from server import" in src:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"модули импортируют монолит: {offenders}"


def test_docs_never_tell_anyone_to_run_the_monolith():
    """Команда запуска в документации — это инструкция, которой следуют.
    `uvicorn server:app` в README поднимает незакрытую сборку у любого,
    кто прочитает README буквально."""
    offenders = []
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "server:app" in line:
                offenders.append(f"{name}:{number}: {line.strip()}")
    assert not offenders, "документация запускает монолит:\n" + "\n".join(offenders)


def test_docs_describe_the_two_process_layout():
    """Два процесса из одного образа — не деталь, а причина, по которой
    приложение вообще может масштабироваться: долгоживущие Telethon-клиенты
    есть только у воркера (иначе AUTH_KEY_DUPLICATED). Документация,
    молчащая об этом, приводит ко второму web-процессу и убитым сессиям."""
    for name in DOCS:
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "app.main:app" in src, f"{name}: не описан запуск web-процесса"
        assert "app.worker" in src, f"{name}: не описан воркер"
