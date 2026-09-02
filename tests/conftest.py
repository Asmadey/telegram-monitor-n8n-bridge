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


@pytest.fixture
def _env(monkeypatch):
    """Секреты для подписи cookie. DATABASE_URL не нужен: get_db переопределён."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-app")
    monkeypatch.setenv("ENVIRONMENT", "development")
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


@pytest_asyncio.fixture
async def anon_client(app, db_engine):
    """Клиент без cookie: аноним, каким его видит каждый роутер."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override
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