"""Единственная точка чтения конфигурации (задача 1.2 PLAN.md).

Контракт (закреплён tests/test_11_config.py):
- все настройки читаются из окружения (pydantic-settings), локально — из .env;
- .env читается, но НИКОГДА не перезаписывается приложением;
- get_settings() кэшируется — одна настройка на процесс.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env лежит в корне приложения (FastAPI/), а не в cwd, откуда запущен pytest.
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    """Настройки приложения. Значения берутся из ENV, затем из .env."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        # В .env есть переменные, которых нет в этой модели (ключи Telegram,
        # токены n8n и т.п.) — ругаться на них нельзя.
        extra="ignore",
    )

    # Инфраструктура. database_url БЕЗ дефолта: забыли URL — падаем громко,
    # а не молча уходим в боевую storage.db (урок 2026-09-01).
    database_url: str = ""
    secret_key: str = ""  # подпись сессий (Phase 2)
    app_encryption_key: str = (
        ""  # Fernet-ключ для шифрования секретов тенантов (Phase 2)
    )

    # Telegram MTProto (свои ключи приложения, не тенантов)
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session_path: str = "teleton_session"

    # Среда
    environment: Literal["development", "production"] = "development"

    # Почта (задача 2.9). В production письма сброса уходят через Resend;
    # без ключа — громкий отказ, не молчание. В dev письма пишутся в
    # mail_dev_dir (letter_opener), реальных отправок из dev нет.
    resend_api_key: str = ""
    mail_from: str = "Teleton <onboarding@resend.dev>"
    app_base_url: str = ""  # база для ссылок в письмах
    mail_dev_dir: str = "tmp/mail"  # dev-аутбокс; tmp/ в .gitignore

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Кэшированная настройка: случайные разные инстансы расползаются."""
    return Settings()
