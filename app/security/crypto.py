"""Шифрование секретов тенантов (задачи 3.2/3.4 PLAN.md) на Fernet.

Ключ — APP_ENCRYPTION_KEY из ENV (pydantic-settings), в БД его нет
никогда. Ключа нет — громкий отказ: «работаем без шифрования» здесь
не бывает, на сервере оседают MTProto-сессии чужих Telegram-аккаунтов,
которые нельзя сбросить удалённо.

validate_encryption_key вызывается при старте приложения (app/main.py):
без явного отказа кто-нибудь однажды запустит прод с ключом по
умолчанию (или «key») — и все сессии окажутся под ним.
"""

from cryptography.fernet import Fernet

from app.config import get_settings


def validate_encryption_key() -> None:
    """Отказ старта, если ключа нет или он невалиден (короткий/битый).

    Вызывается при импорте app.main — приложение не поднимается вовсе.
    """
    key = get_settings().app_encryption_key
    if not key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY не задан — приложение не стартует: "
            "секреты тенантов (MTProto-сессии, ключи интеграций) "
            "шифруются этим ключом"
        )
    try:
        Fernet(key.encode())
    except ValueError as e:
        raise RuntimeError(
            f"APP_ENCRYPTION_KEY невалиден (короткий/битый) — приложение "
            f"не стартует: {e}"
        ) from e


def _fernet() -> Fernet:
    key = get_settings().app_encryption_key
    if not key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY не задан: секреты тенантов нечем шифровать"
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
