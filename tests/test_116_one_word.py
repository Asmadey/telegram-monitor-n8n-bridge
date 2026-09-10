"""Одно слово про один объект (задача 11.8).

Про одну и ту же сущность в проекте говорили тремя способами: таблица
`monitors`, API `/api/monitors`, интерфейс «Источники», а журнал писал
«монитор %s». Пользователь видел два разных слова про один объект и не мог
знать, что это одно и то же.

Имя таблицы остаётся внутренним — переименование стоит дороже, чем даёт.
Но всё, что видит пользователь — интерфейс, журнал, тексты ошибок, — с
этой задачи говорит «источник».

Заодно снимается устаревший `/api/monitors`: интерфейс на него больше не
ходит, а два API над одной таблицей неизбежно расходятся. Это уже
случилось: 11.4 перевела чтение на новую таблицу, запись осталась на
старой, и добавленный канал молчал без единой ошибки (11.6а).
"""

import pathlib
import re

import pytest
from conftest import act_as

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Файлы, чей текст доходит до пользователя: разметка, модули интерфейса и
# сообщения, которые пишутся в журнал или в тело ошибки.
USER_FACING = [
    ROOT / "static" / "index.html",
    *sorted((ROOT / "static" / "js").glob("*.js")),
    ROOT / "app" / "api" / "sources.py",
    ROOT / "app" / "worker.py",
]

# «монитор» в любом падеже и роде
MONITOR_WORD = re.compile(r"монитор(?![аеиоуыъья]?\w*ing)\w*", re.IGNORECASE)


def _user_visible_lines(path: pathlib.Path) -> list[str]:
    """Строки, которые видит пользователь: разметка целиком, а из кода —
    только строковые литералы (комментарии и докстринги пишутся для
    разработчика, и в них история терминов уместна)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        # из JS убираем комментарии: там тоже пишут для себя
        text = re.sub(r"//[^\n]*", "", text)
        return text.splitlines()
    import ast

    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            literals.append(node.value)
    return literals


def test_user_facing_text_says_source_not_monitor():
    offenders = []
    for path in USER_FACING:
        if not path.exists():
            continue
        for line in _user_visible_lines(path):
            if MONITOR_WORD.search(line):
                offenders.append(f"{path.name}: {line.strip()[:90]}")
    assert not offenders, (
        "пользователь видит два разных слова про один объект:\n"
        + "\n".join(offenders[:12])
    )


def test_legacy_monitors_api_is_gone():
    """Два API над одной таблицей расходятся — это уже случилось (11.6а)."""
    assert not (ROOT / "app" / "api" / "monitors.py").exists(), (
        "устаревший модуль остался: следующая правка снова разойдётся по "
        "двум путям записи"
    )
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "monitors" not in main, "роутер монолитных каналов ещё подключён"


@pytest.mark.asyncio
async def test_legacy_routes_answer_nothing(anon_client, db, user):
    await act_as(anon_client, db, user)
    assert (await anon_client.get("/api/monitors")).status_code == 404


@pytest.mark.asyncio
async def test_dedup_can_be_reset_for_a_source(anon_client, db, user):
    """Переразбор старых постов — единственный законный способ вернуть их.

    Он был у канала (`/api/monitors/{id}/reset-dedup`) и обязан остаться у
    источника: без него улучшенный промпт нечем применить к тому, что уже
    прочитано.
    """
    await act_as(anon_client, db, user)
    created = await anon_client.post(
        "/api/sources", json={"title": "Вакансии", "interval_minutes": 60}
    )
    public_id = created.json()["public_id"]

    reset = await anon_client.post(f"/api/sources/{public_id}/reset-dedup")
    assert reset.status_code == 200, reset.text
