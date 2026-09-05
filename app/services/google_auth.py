"""Верификация Google ID-токенов (Фаза 6 PLAN.md).

Firebase — провайдер идентичности, НЕ система сессий: verify_id_token
проверяет подпись/aud/exp, пользователь ищется или создаётся в СВОЕЙ
таблице, сессия — своя cookie. Так остаётся контроль над отзывом
(блокировка юзера убивает сессии), работает админка, и GitHub/Apple
добавятся без переписывания аутентификации.

Верификатор — инъектируемая зависимость (как Telethon-клиент в 3.3):
тесты (test_60) подменяют её фейком, живой Firebase в CI не нужен.

firebase_admin импортируется ЛЕНИВО при первом обращении: web-процесс
без единого POST /auth/google не должен требовать конфигурации
Firebase (локальная разработка). Но сама верификация без конфигурации
падает ГРОМКО (RuntimeError → 500, не маскировка под 401): урок
2026-09-02 — misconfiguration не должна выглядеть успехом/отказом входа.
"""

from collections.abc import Callable

# Контракт верификатора: строка токена → dict claims; невалидный/
# просроченный/чужой aud — ЛЮБОЕ исключение (живой firebase_admin
# поднимает InvalidIdTokenError, подкласс ValueError)
IdTokenVerifier = Callable[[str], dict]

_initialized = False


def _ensure_initialized() -> None:
    """Ленивая инициализация firebase_admin (один раз за процесс).
    GOOGLE_APPLICATION_CREDENTIALS читает библиотека сама."""
    global _initialized
    if _initialized:
        return
    import firebase_admin

    if not firebase_admin.apps:
        firebase_admin.initialize_app()
    _initialized = True


def _live_verifier(token: str) -> dict:
    try:
        _ensure_initialized()
    except Exception as e:  # noqa: BLE001 — нет кредов/проекта: громко, не 401
        raise RuntimeError(
            "Firebase не сконфигурирован (GOOGLE_APPLICATION_CREDENTIALS): "
            f"verify_id_token невозможен — {e}"
        ) from e
    from firebase_admin import auth as fb_auth

    return fb_auth.verify_id_token(token)


def get_google_verifier() -> IdTokenVerifier:
    """Зависимость FastAPI: живой verify_id_token; тесты подменяют."""
    return _live_verifier
