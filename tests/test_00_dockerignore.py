"""Задача 0.1 — секреты не должны попадать в Docker-образ.

Dockerfile делает `COPY . .`, поэтому всё, чего нет в .dockerignore, оказывается
внутри образа. .gitignore здесь не помогает — это другой файл с другим списком.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PATTERNS = {
    ".env",
    "*.session",
    "storage.db",
    "key.md",
    "Ruby/",
    "exports/",
    ".git/",
    # Добавлено 2026-09-10, когда дерево развернули: `private-backups/` лежал
    # ЭТАЖОМ ВЫШЕ репозитория и в контекст сборки не попадал вовсе. После
    # разворачивания он оказался внутри контекста, а внутри него — снимок
    # `personal_account.session` и боевой `storage.db`. Перенос каталога
    # молча превратил безопасный файл в содержимое образа.
    "private-backups/",
}


def _patterns() -> set[str]:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_dockerignore_covers_all_secrets():
    missing = REQUIRED_PATTERNS - _patterns()
    assert not missing, f"В .dockerignore отсутствуют шаблоны: {sorted(missing)}"


def test_dockerfile_still_copies_everything():
    """Страховка: если COPY . . заменят на точечный список, этот тест напомнит
    пересмотреть .dockerignore — но пока он такой, список обязателен."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . ." in dockerfile, (
        "Dockerfile больше не копирует каталог целиком — пересмотрите "
        "REQUIRED_PATTERNS в этом тесте."
    )
