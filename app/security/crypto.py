"""Шифрование секретов тенантов (задачи 3.2/3.4 PLAN.md) на Fernet.

Ключ — APP_ENCRYPTION_KEY из ENV (pydantic-settings), в БД его нет
никогда. Ключа нет — громкий отказ: «работаем без шифрования» здесь
не бывает, на сервере оседают MTProto-сессии чужих Telegram-аккаунтов,
которые нельзя сбросить удалённо.

Полнота контракта (отказ старта без ключа, шифрование ключей
интеграций) — задача 3.4; здесь фундамент: encrypt/decrypt.
"""

from cryptography.fernet import Fernet

from app.config import get_settings


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
