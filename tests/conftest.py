"""Общие фикстуры behavioral-тестов (Фаза 2).

Тестовое приложение живёт на временной aiosqlite-базе: get_db переопределён
на движок фикстуры, живой Postgres не нужен. httpx.ASGITransport вместо
sync-TestClient: запросы и БД работают в одном event loop — иначе aiosqlite
падает «attached to a different loop».
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import get_db
from app.models import Base, User
from app.security.csrf import CSRF_COOKIE, CSRF_HEADER, SAFE_METHODS


class FrontendLikeClient(AsyncClient):
    """Клиент, ведущий себя как наш фронтенд: читает csrf-cookie и шлёт её
    значение в заголовке на каждом не-GET запросе (задача 2.6).

    Тесты 23/24 проходят «как браузер» и не думают про CSRF; негативные
    CSRF-тесты (25) используют raw_client без авто-инъекции.
    """

    async def request(self, method, url, **kwargs):
        token = self.cookies.get(CSRF_COOKIE)
        if token and method.upper() not in SAFE_METHODS:
            headers = dict(kwargs.get("headers") or {})
            headers[CSRF_HEADER] = token
            kwargs["headers"] = headers
        return await super().request(method, url, **kwargs)


def walk_routes(routes):
    """FastAPI 0.141 оборачивает include_router в _IncludedRouter без .path:
    реальные маршруты лежат в original_router.routes — разворачиваем рекурсивно."""
    for route in routes:
        sub = getattr(route, "original_router", None)
        if sub is not None:
            yield from walk_routes(sub.routes)
        else:
            yield route


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """In-memory счётчики (slowapi + password-reset) живут в процессе —
    без сброса один тест выжигал бы лимит на весь сеанс (11 логинов в
    test_26 блокировали бы входы в test_23/24)."""
    from app.security.ratelimit import reset_all

    yield
    reset_all()


@pytest.fixture
def _env(monkeypatch, tmp_path):
    """Секреты для подписи cookie. DATABASE_URL не нужен: get_db переопределён.
    MAIL_DEV_DIR — в tmp теста: dev-письма (задача 2.9) не должны сыпаться
    в tmp/ репозитория во время прогона."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-app")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MAIL_DEV_DIR", str(tmp_path / "mail"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_engine(_env, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/app_test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def app(_env):
    # импорт внутри фикстуры: env должен стоять до первого get_settings()
    from app.main import app as fastapi_app

    return fastapi_app


def _override_get_db(db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override():
        async with async_session() as session:
            yield session

    return _override


@pytest_asyncio.fixture
async def anon_client(app, db_engine):
    """Клиент «как браузер»: csrf-заголовок проставляется автоматически.

    Прайминг GET /health имитирует первую загрузку страницы — без него
    сервер ещё не выдал csrf-cookie, и первый POST был бы 403 даже у
    честного клиента.
    """
    app.dependency_overrides[get_db] = _override_get_db(db_engine)
    transport = ASGITransport(app=app)
    async with FrontendLikeClient(transport=transport, base_url="http://test") as ac:
        await ac.get("/health")
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def raw_client(app, db_engine):
    """Клиент без авто-инъекции csrf — негативные тесты задачи 2.6."""
    app.dependency_overrides[get_db] = _override_get_db(db_engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def db(db_engine):
    """Сессия для посева данных (создание юзера, сессии) — вне HTTP."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def user(db):
    u = User(email="owner@example.com")
    db.add(u)
    await db.commit()
    return u
