"""Задача 1.2 — единственная точка чтения ENV, .env больше не перезаписывается.

До задачи 1.2 POST /api/settings перезаписывал .env целиком, оставляя в нём
2–3 строки (server.py, update_env_file). На Railway ФС эфемерна — настройка
молча терялась при каждом редеплое. Контракт: конфиг читается из окружения
через pydantic-settings, и ни один модуль приложения не пишет .env.
"""
import importlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "x" * 44)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    config = importlib.import_module("app.config")
    importlib.reload(config)
    s = config.Settings()
    assert s.database_url.startswith("postgresql+asyncpg://"), (
        f"database_url читается не из ENV: {s.database_url!r}"
    )
    assert s.app_encryption_key == "x" * 44


def test_settings_environment_and_prod_flag(monkeypatch):
    config = importlib.import_module("app.config")
    importlib.reload(config)
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = config.Settings()
    assert s.is_production is True


def test_get_settings_is_cached(monkeypatch):
    """Одна настройка на процесс: случайные разные инстансы расползаются."""
    config = importlib.import_module("app.config")
    importlib.reload(config)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "x" * 44)
    assert config.get_settings() is config.get_settings()


def test_app_never_writes_env_file():
    """update_env_file не существует ни в app/, ни в server.py,
    и никакой модуль не открывает .env на запись."""
    bad_open = re.compile(r"open\(\s*ENV_FILE")
    for f in list((ROOT / "app").rglob("*.py")) + [ROOT / "server.py"]:
        src = f.read_text(encoding="utf-8")
        assert "update_env_file" not in src, f"{f.name}: update_env_file жив"
        assert not bad_open.search(src), f"{f.name}: пишет .env напрямую"


@pytest.mark.parametrize("env_file", [ROOT / ".env"])
def test_env_file_is_not_tracked_by_git(env_file):
    """Сам файл .env существует локально, но не должен попасть в коммит."""
    ignore = ROOT / ".gitignore"
    if not ignore.exists():
        pytest.skip("нет .gitignore — проверка не применима")
    patterns = {
        line.strip() for line in ignore.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in patterns, ".gitignore не исключает .env"