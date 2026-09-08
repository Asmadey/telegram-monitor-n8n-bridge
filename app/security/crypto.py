"""Шифрование секретов тенантов (задачи 3.2/3.4 PLAN.md) на Fernet.

Ключ — APP_ENCRYPTION_KEY из ENV (pydantic-settings), в БД его нет
никогда. Ключа нет — громкий отказ: «работаем без шифрования» здесь
не бывает, на сервере оседают MTProto-сессии чужих Telegram-аккаунтов,
которые нельзя сбросить удалённо.

validate_encryption_key вызывается при старте приложения (app/main.py):
без явного отказа кто-нибудь однажды запустит прод с ключом по
умолчанию (или «key») — и все сессии окажутся под ним.
"""

import hashlib

from cryptography.fernet import Fernet, InvalidToken

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


class EncryptionKeyMismatch(InvalidToken):
    """Данные зашифрованы другим ключом, чем тот, которым их читают.

    Подкласс InvalidToken намеренно: контракт 3.4 («дешифровка чужим ключом
    не проходит молча») держится прежними тестами, а перехватывающий код не
    обязан знать о новом имени. Меняется только сообщение — у голого
    InvalidToken его нет вовсе, и в журнале оставалась строка
    «Ошибка извлечения: InvalidToken», требующая знать устройство Fernet.
    """


def key_fingerprint(key: str | None = None) -> str:
    """Короткий отпечаток ключа: первые 8 знаков SHA-256.

    Нужен, чтобы сравнивать ключи двух процессов, не показывая их. Выяснение
    того, одинаков ли APP_ENCRYPTION_KEY у web и воркера, 7 сентября заняло
    несколько кругов переписки: значение секретное, единственным симптомом
    был InvalidToken в журнале раз в тридцать секунд. По восьми знакам хеша
    ключ не восстановить, а увидеть расхождение — достаточно.
    """
    value = get_settings().app_encryption_key if key is None else key
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


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
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Единственная реальная причина в этой сборке: APP_ENCRYPTION_KEY
        # разошёлся между процессами. Web шифрует своим ключом, воркер
        # читает своим — и снаружи это выглядит как «мониторинг не
        # работает», хотя вход и интерфейс исправны (7 сентября).
        raise EncryptionKeyMismatch(
            "Данные зашифрованы другим ключом: APP_ENCRYPTION_KEY у этого "
            "процесса не совпадает с тем, которым их сохранили. Проверьте, "
            "что у сервисов web и worker переменная задана ОДНИМ значением; "
            "менять её после первого запуска нельзя — старые записи станут "
            "нечитаемыми."
        ) from exc
