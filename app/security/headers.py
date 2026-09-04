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

# HSTS: год; includeSubDomains не ставим до своего домена — на сервисном
# *.up.railway.app он распространялся бы на поддомены сервиса без нужды.
HSTS = "max-age=31536000"

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": HSTS,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


async def add_security_headers(request: Request, call_next) -> Response:
    """Каждый ответ — включая 404/429 и ошибки сервера: заголовки вешает
    middleware, а не отдельные эндпоинты."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response
