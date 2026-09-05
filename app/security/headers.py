"""Заголовки безопасности (задача 7.2 PLAN.md) — middleware, не эндпоинты.

CSP со script-src 'self' без 'unsafe-inline' стала возможна после 5.1
(инлайн-скрипты вырезаны, все обработчики — в ES-модулях) и 5.3 (страницы
входа грузят внешний auth-pages.js). Это второй рубеж после экранирования
5.2: даже прорвавшийся XSS не исполнится — браузер откажется запускать
инлайн-скрипт.

style-src несёт 'unsafe-inline' осознанно: страницы входа (5.3) держат
инлайн-СТИЛИ; стили не исполняют код. img-src с data: — аватарки-заглушки
и встраиваемые картинки, исполнить код через data:-картинку нельзя.
"""

from fastapi import Request, Response

from app.config import get_settings

# default-src замыкает всё неперечисленное; каждая директива — 'self' либо
# явный запрет: внешний origin у фронта нет (сборки и CDN не используются).
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Origin, без которых вход через Google физически не работает: SDK лежит на
# gstatic, обмен токенами идёт в identitytoolkit/securetoken, окно согласия
# рисует accounts.google.com в iframe. Список открывается ТОЛЬКО когда
# Firebase настроен: пускать сторонний скриптовый origin ради выключенной
# функции — это платить ослаблением политики ни за что.
GOOGLE_SCRIPT_SRC = ("https://www.gstatic.com", "https://apis.google.com")
GOOGLE_CONNECT_SRC = (
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
)
GOOGLE_FRAME_SRC = ("https://accounts.google.com",)


def build_csp(*, google_auth_domain: str = "") -> str:
    """CSP под текущую конфигурацию.

    'unsafe-inline' в script-src не появляется ни при каком раскладе — это
    второй рубеж после экранирования (5.2), и Firebase его не требует.
    """
    if not google_auth_domain:
        return CSP
    frame = (*GOOGLE_FRAME_SRC, f"https://{google_auth_domain}")
    return (
        CSP.replace(
            "script-src 'self'", "script-src 'self' " + " ".join(GOOGLE_SCRIPT_SRC)
        ).replace(
            "connect-src 'self'", "connect-src 'self' " + " ".join(GOOGLE_CONNECT_SRC)
        )
        + "; frame-src "
        + " ".join(frame)
    )


def current_csp() -> str:
    settings = get_settings()
    return build_csp(
        google_auth_domain=(
            settings.firebase_auth_domain if settings.google_sign_in_enabled else ""
        )
    )


# HSTS: год; includeSubDomains не ставим до своего домена — на сервисном
# *.up.railway.app он распространялся бы на поддомены сервиса без нужды.
HSTS = "max-age=31536000"

# CSP здесь НЕТ намеренно: она зависит от конфигурации и считается на
# запрос (current_csp). Статическая запись в этом словаре молча перекрывала
# бы вычисленную — ловушка, пойманная на красной фазе.
SECURITY_HEADERS = {
    "Strict-Transport-Security": HSTS,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


async def add_security_headers(request: Request, call_next) -> Response:
    """Каждый ответ — включая 404/429 и ошибки сервера: заголовки вешает
    middleware, а не отдельные эндпоинты."""
    response = await call_next(request)
    # CSP считается на запрос: набор разрешённых origin зависит от того,
    # настроен ли вход через Google (см. build_csp)
    response.headers["Content-Security-Policy"] = current_csp()
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response
