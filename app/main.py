"""Сборка FastAPI (Фаза 2, целевая структура PLAN.md раздел 2).

server.py остаётся точкой входа локальной разработки, пока его код не
перенесён в модули (Фазы 3–4); редактировать его после Фазы 1 нельзя —
переносить. Эта сборка — та, что уедет на Railway.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, public

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