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


# --------------------------------------------------------------------------
# Найдено при ревью 7.3: PLAN.md существовал в двух копиях. Живая лежала вне
# git (корень репозитория — FastAPI/), версионированная была заморожена на
# 0 из 23 закрытых чекбоксов при 17 реально закрытых. Клонировавший репозиторий
# агент получал заведомо неверный статус. AGENTS.md §9 — статус в одном месте.
# --------------------------------------------------------------------------

PLAN = APP / "docs" / "PLAN.md"


def test_plan_in_repo_is_the_maintained_one():
    """Версионированный план обязан нести таблицу статусов: именно её
    наличие отличает живой документ от замороженной копии."""
    assert PLAN.exists(), "docs/PLAN.md отсутствует"
    text = PLAN.read_text(encoding="utf-8")
    assert "## Статус выполнения" in text, (
        "в версионированном плане нет таблицы «Статус выполнения» — "
        "значит поддерживается какая-то другая копия"
    )
    assert text.count("- [x]") > 0, (
        "ни одна задача не отмечена закрытой — план заморожен на состоянии "
        "при создании (так уже было: 0 из 23 при 17 реально закрытых)"
    )


def test_no_competing_plan_copy_above_the_repo():
    """Файл выше корня репозитория допустим только как указатель.

    Пропускается там, где родительского каталога нет (CI клонирует
    репозиторий, а не рабочее дерево оператора).
    """
    outer = REPO / "PLAN.md"
    if not outer.exists():
        return
    lines = outer.read_text(encoding="utf-8").count("\n")
    assert lines < 100, (
        f"{outer.name} выше репозитория снова стал полной копией плана "
        f"({lines} строк) — статус разъедется молча, как в прошлый раз"
    )
