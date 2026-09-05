"""Хеширование паролей на bcrypt (задача 2.1 PLAN.md).

Отступление от плана, зафиксированное здесь: план предписывал passlib, но
passlib не поддерживается с 2020 и несовместим с bcrypt>=4.1 — падает в
своей внутренней детекции бэкенда (detect_wrap_bug хеширует 73-байтный
секрет, современный bcrypt отказывается молча усекать). Поэтому хешируем
библиотекой `bcrypt` напрямую: тот же алгоритм, тот же формат $2b$,
контракт tests/test_20_passwords.py не меняется.

bcrypt — соль на каждый вызов, медленный по назначению.
Слой отказывается хешировать пароли длиннее 72 байт: bcrypt молча
усекает их до префикса, и два разных пароля с общим началом становятся
одним паролем. Отказ здесь (ValueError) → на регистрации он станет
честной 422, а не молчаливой потерей хвоста пароля.
"""

import bcrypt

# bcrypt имеет жёсткий лимит входа в 72 байта (сам молчит и усекает)
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    data = plain.encode("utf-8")
    if len(data) > _BCRYPT_MAX_BYTES:
        raise ValueError(
            f"пароль длиннее {_BCRYPT_MAX_BYTES} байт: bcrypt молча усекает до префикса"
        )
    return bcrypt.hashpw(data, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    data = plain.encode("utf-8")
    if len(data) > _BCRYPT_MAX_BYTES:
        # пароль, который невозможно захешировать, не может быть верным;
        # отдельный raise здесь дал бы брутфорсу оракул длины
        return False
    try:
        return bcrypt.checkpw(data, hashed.encode("utf-8"))
    except ValueError:
        # битый/чужой формат хеша не должен ронять логин — он просто неверен
        return False
