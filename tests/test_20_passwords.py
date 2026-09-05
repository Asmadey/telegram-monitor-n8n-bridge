"""Задача 2.1 — хеширование паролей на bcrypt.

Пароль в базе — единственный секрет, который нельзя «перевыпустить» удалённо:
его утекшая копия работает, пока пользователь не сменит пароль сам. Поэтому
хеш обязан быть bcrypt ($2b$), с солью на каждый вызов.
"""

from app.security.passwords import hash_password, verify_password

PLAIN = "correct horse battery staple"


def test_hash_is_bcrypt_not_plaintext():
    h = hash_password(PLAIN)
    assert h != PLAIN, "хеш равен паролю"
    assert h.startswith("$2b$"), f"не bcrypt-формат: {h[:7]}"


def test_verify_accepts_correct_and_rejects_wrong():
    h = hash_password(PLAIN)
    assert verify_password(PLAIN, h) is True
    assert verify_password("wrong password", h) is False


def test_salt_makes_hashes_different():
    """Одинаковые пароли → разные хеши: соль не фиксированная."""
    assert hash_password(PLAIN) != hash_password(PLAIN)


def test_bcrypt_truncation_is_refused():
    """bcrypt молча режет всё после 72 байт: пароль «aaa…A» и «aaa…B» с общим
    началом стали бы ОДНИМ паролем. Слой хеширования обязан отказывать
    громко (ValueError) — молчаливое усечение это не пароль, а его префикс."""
    import pytest

    with pytest.raises(ValueError):
        hash_password("x" * 73)
