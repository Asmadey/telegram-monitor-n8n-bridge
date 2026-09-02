"""Сборка FastAPI (Фаза 2, целевая структура PLAN.md раздел 2).

server.py остаётся точкой входа локальной разработки, пока его код не
перенесён в модули (Фазы 3–4); редактировать его после Фазы 1 нельзя —
переносить. Эта сборка — та, что уедет на Railway.
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, public
from app.security.csrf import (
    CSRF_COOKIE,
    SAFE_METHODS,
    issue_csrf_cookie,
    verify_csrf,
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Схема API наружу не отдаётся анониму: /openapi.json, /docs, /redoc —
# бесплатная карта поверхности атаки (их перечисляет test_22 как маршруты).
# Если документация понадобится — открывать только за require_user.
app = FastAPI(title="Teleton", openapi_url=None, docs_url=None, redoc_url=None)

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(auth.public_router)  # signup/login — явный opt-out (2.4)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    """Каждый не-GET без валидного X-CSRF-Token — 403 (задача 2.6).

    Проверка ВЫШЕ авторизации: CSRF не заботится, кто ты — только кто
    подделал запрос. Новым посетителям тут же выдаётся anon-токен: браузер
    грузит страницу (GET) и уже с неё шлёт POST-ы с заголовком.
    """
    if request.method not in SAFE_METHODS and not verify_csrf(request):
        return JSONResponse(
            {"detail": "CSRF-токен отсутствует или неверен"}, status_code=403
        )
    response = await call_next(request)
    if CSRF_COOKIE not in request.cookies:
        issue_csrf_cookie(response)  # anon-токен; при логине перевыпустится
    return response