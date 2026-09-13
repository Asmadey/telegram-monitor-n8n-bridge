"""Задача 1.1 — все зависимости из requirements.txt импортируются.

Имена модулей соответствуют пакетам: sqlalchemy[asyncio] → sqlalchemy,
pydantic-settings → pydantic_settings, passlib[bcrypt] → passlib и т.д.
"""

import importlib

import pytest

REQUIRED_MODULES = [
    # исходные зависимости
    "telethon",
    "dotenv",
    "fastapi",
    "uvicorn",
    "pydantic",
    "httpx",
    # Фаза 1 (задача 1.1)
    "sqlalchemy",
    "sqlalchemy.ext.asyncio",
    "asyncpg",
    "aiosqlite",
    "alembic",
    "pydantic_settings",
    "bcrypt",
    "itsdangerous",
    "email_validator",
    "cryptography.fernet",
    "slowapi",
    # dev-зависимости
    # rich переехал сюда из боевых (12.2): им пользуются только CLI-скрипты
    # в корне. tabulate убран совсем — его не импортировал никто.
    "rich",
    "pytest",
    "pytest_asyncio",
    "ruff",
    "mypy",
    "bandit",
]


@pytest.mark.parametrize("module", REQUIRED_MODULES)
def test_dependency_is_importable(module):
    importlib.import_module(module)
