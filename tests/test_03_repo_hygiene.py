"""Задача 0.5 — в репозитории нет мусора и дублей.

FastAPI/Ruby/ — вторая копия Rails-шаблона внутри Python-проекта (живой образец
остаётся в Teleton/Ruby/). Teleton/storage.db — пустой файл-дубль: приложение
однажды запишет данные не туда. monitors.json.bak — остаток старой миграции.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # Teleton/
APP = REPO / "FastAPI"

GONE = [
    APP / "Ruby",  # дубль Rails-шаблона
    REPO / "storage.db",  # пустой файл в корне
    APP / "monitors.json.bak",  # остаток старой миграции
]


def test_junk_paths_do_not_exist():
    survivors = [str(p.relative_to(REPO)) for p in GONE if p.exists()]
    assert not survivors, f"В репозитории остался мусор: {survivors}"


def test_app_storage_db_is_the_real_one():
    """Живая база остаётся на месте и не пуста (защита от случайной уборки не того файла)."""
    db = APP / "storage.db"
    assert db.exists(), "FastAPI/storage.db исчез — удалён не тот файл"
    assert db.stat().st_size > 0, "FastAPI/storage.db пуст"
