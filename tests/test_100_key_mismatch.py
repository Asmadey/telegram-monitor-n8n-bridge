"""Несовпадение ключа шифрования объясняет себя само (9.12).

Найдено на живом деплое 7 сентября. Воркер каждые 30 секунд писал в журнал
«Ошибка извлечения: InvalidToken» — и это всё. Между тем у `InvalidToken`
из Fernet значение ровно одно: данные зашифрованы НЕ ТЕМ ключом, которым их
сейчас пытаются прочитать.

В этой архитектуре у такого отказа единственная реальная причина:
`APP_ENCRYPTION_KEY` у web и у воркера разные. Web зашифровал
MTProto-сессию своим ключом, воркер читает своим — и не может. Снаружи это
выглядит как «мониторинг не работает», хотя вход, каналы и интерфейс
исправны: два процесса просто говорят на разных ключах.

Сообщение обязано называть причину и действие. Диагностика, требующая
знать устройство Fernet, — это не диагностика.

Тип остаётся подклассом `InvalidToken`: контракт 3.4 («дешифровка чужим
ключом не проходит молча») продолжает держаться теми же тестами, а
перехватывающий код не обязан знать о новом имени.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.security.crypto import EncryptionKeyMismatch, decrypt, encrypt


@pytest.fixture
def other_key(monkeypatch):
    """Данные, зашифрованные ЧУЖИМ ключом (как web при другом ключе)."""
    from app.config import get_settings

    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    token = encrypt("1BQANOTE-сессия-аккаунта")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield token
    get_settings.cache_clear()


def test_mismatch_says_what_happened_and_what_to_do(other_key):
    with pytest.raises(EncryptionKeyMismatch) as failure:
        decrypt(other_key)

    text = str(failure.value).lower()
    assert "app_encryption_key" in text, "не названа переменная, которую менять"
    assert "воркер" in text or "процесс" in text, (
        "не сказано, ГДЕ ключи разошлись — а это единственная реальная "
        "причина такого отказа в этой сборке"
    )


def test_it_is_still_an_invalid_token(other_key):
    """Контракт 3.4 не ослаблен: подделка по-прежнему не проходит молча."""
    with pytest.raises(InvalidToken):
        decrypt(other_key)


def test_normal_decryption_is_untouched(_env):
    # _env даёт ключ так же, как его даёт окружение приложения
    assert decrypt(encrypt("обычное значение")) == "обычное значение"
