"""CORS для раздельного деплоя (фронтенд Vercel ↔ API Railway).

Своя middleware вместо штатной CORSMiddleware по одной причине: список
разрешённых origin читается на КАЖДОМ запросе. Настройка живёт в переменных
окружения Railway, и добавление превью-домена не должно требовать пересборки
образа.

Главное правило зашито в код, а не в конфиг: `Access-Control-Allow-Origin: *`
никогда не сочетается с `Allow-Credentials: true`. Браузер такое сочетание
отвергает — то есть сервер выглядел бы настроенным и не работал, — а если бы
принимал, API с сессионными cookie был бы открыт любому сайту. Поэтому origin
всегда отражается из белого списка поимённо.
"""

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.security.csrf import CSRF_HEADER

ALLOWED_METHODS = "GET, POST, PATCH, DELETE, OPTIONS"
ALLOWED_HEADERS = f"content-type, {CSRF_HEADER}"
MAX_AGE = "600"


class CorsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _allowed(self, origin: str) -> bool:
        return bool(origin) and origin in get_settings().frontend_origin_list

    def _headers(self, origin: str) -> list[tuple[bytes, bytes]]:
        return [
            (b"access-control-allow-origin", origin.encode()),
            (b"access-control-allow-credentials", b"true"),
            # ответ зависит от origin — без Vary кеш отдаст чужой заголовок
            (b"vary", b"Origin"),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin", "")
        if not self._allowed(origin):
            # Чужой origin обслуживается как обычно, но БЕЗ разрешающих
            # заголовков: браузер сам не отдаст ответ странице. Отвечать 403
            # незачем — это сломало бы серверных клиентов, у которых origin нет.
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS" and "access-control-request-method" in Headers(
            scope=scope
        ):
            response = PlainTextResponse("", status_code=204)
            for key, value in self._headers(origin):
                response.headers[key.decode()] = value.decode()
            response.headers["access-control-allow-methods"] = ALLOWED_METHODS
            response.headers["access-control-allow-headers"] = ALLOWED_HEADERS
            response.headers["access-control-max-age"] = MAX_AGE
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.extend(self._headers(origin))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)
