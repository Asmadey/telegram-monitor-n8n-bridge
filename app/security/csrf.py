"""CSRF: double-submit token (задача 2.6).

Сторонний сайт не может (а) прочитать cookie жертвы и (б) поставить свои
заголовки на кросс-доменный запрос — поэтому «cookie == заголовок» уже
закрывает класс атак. Но чисто строковая сверка уязвима к cookie-tossing
(поддомен проставляет СВОЙ csrf-cookie), поэтому токен ещё и подписан, а
после логина — привязан к id сессии: подброшенное значение не пройдёт,
даже если заголовок ему совпадает.
"""
from typing import Optional

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings
from app.security.sessions import SESSION_COOKIE, read_session_id

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_SALT = "csrf"


def _serializer() -> URLSafeSerializer:
    secret = get_settings().secret_key
    if not secret:
        raise RuntimeError("SECRET_KEY не задан: CSRF-токен нечем подписать")
    return URLSafeSerializer(secret, salt=_SALT)


def make_csrf_token(session_id=None) -> str:
    """Токен анонима ({"anon"}) или привязанный к сессии ({"sid": ...})."""
    if session_id is None:
        return _serializer().dumps({"anon": True})
    return _serializer().dumps({"sid": str(session_id)})


def issue_csrf_cookie(response: Response, session_id=None) -> None:
    """НЕ HttpOnly: JS обязан читать cookie и слать её в X-CSRF-Token."""
    response.set_cookie(
        CSRF_COOKIE,
        make_csrf_token(session_id),
        httponly=False,
        secure=get_settings().is_production,
        samesite="lax",
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/")


def verify_csrf(request: Request) -> bool:
    """Cookie == заголовок, подпись верна, привязка к сессии сходится.

    Любой сбой — False; почему именно отклонено, наружу не сообщаем.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or cookie != header:
        return False
    try:
        payload = _serializer().loads(cookie)
    except (BadSignature, ValueError, TypeError):
        return False
    session_id: Optional[object] = read_session_id(
        request.cookies.get(SESSION_COOKIE, "")
    )
    if session_id is None:
        # cookie сессии нет (не залогинен или истекла). Привязку проверять
        # не с чем, а остаться с 403 навсегда пользователь не должен: у
        # истёкшей сессии csrf-cookie всё ещё «привязан» к мёртвому sid.
        # Подписанной cookie достаточно: подброшенное извне значение не
        # пройдёт подпись, а кастомный заголовок кросс-доменно не поставить.
        return True
    return payload.get("sid") == str(session_id)