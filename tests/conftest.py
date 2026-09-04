"""Общие фикстуры behavioral-тестов (Фаза 2).

Тестовое приложение живёт на временной aiosqlite-базе: get_db переопределён
на движок фикстуры, живой Postgres не нужен. httpx.ASGITransport вместо
sync-TestClient: запросы и БД работают в одном event loop — иначе aiosqlite
падает «attached to a different loop».
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import get_db
from app.models import Base, User
from app.security.csrf import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SAFE_METHODS,
    make_csrf_token,
)
from app.security.sessions import (
    SESSION_COOKIE,
    create_session,
    sign_session_id,
)


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
    в tmp/ репозитория во время прогона. APP_ENCRYPTION_KEY — случайный
    Fernet-ключ per-run: тестовое значение, не секрет (генерится здесь же,
    а не захардкожено, чтобы в репозитории не было НИ одного ключеподобного
    значения)."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-app")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MAIL_DEV_DIR", str(tmp_path / "mail"))
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
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


async def _new_user(db, email: str) -> User:
    u = User(email=email)
    db.add(u)
    await db.commit()
    return u


@pytest_asyncio.fixture
async def user_a(db):
    """Владелец ресурсов (тенант A) для тестов изоляции (задача 3.1)."""
    return await _new_user(db, "tenant-a@example.com")


@pytest_asyncio.fixture
async def user_b(db):
    """Другой пользователь (тенант B): не должен видеть данные A."""
    return await _new_user(db, "tenant-b@example.com")


async def act_as(client, db, user) -> None:
    """Клиент действует от имени юзера: свежая сессия в cookie.

    Сессия создаётся прямо в БД (минуя login), поэтому csrf-токен выдаём
    так же, как его выдаёт _open_session при логине: ПОДПИСАННЫЙ и
    привязанный к sid (анон-токен с живой cookie сессии не проходит —
    так устроен verify_csrf, задача 2.6). Прежние cookie (домен test от
    прайминга GET /health) вычищаем из jar — httpx падает CookieConflict
    при двух cookie с одним именем. Вынесена из test_32 (3.3), когда
    появился второй потребитель: свип изоляции в test_30 и лента в
    test_50 (задача 5.4).

    httpx нормализует base_url http://test в хост test.local — cookie,
    поставленная на домен "test", на запрос просто не уедет (403 CSRF).
    """
    session = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    for c in list(client.cookies.jar):
        if c.name in (CSRF_COOKIE, SESSION_COOKIE):
            client.cookies.jar.clear(c.domain, c.path, c.name)
    client.cookies.set(
        SESSION_COOKIE, sign_session_id(session.id), domain="test.local", path="/"
    )
    client.cookies.set(
        CSRF_COOKIE, make_csrf_token(session.id), domain="test.local", path="/"
    )


@pytest.fixture
def alembic_target_db(monkeypatch):
    """Направляет Alembic на TEST_DATABASE_URL и отдаёт этот URL.

    alembic/env.py принципиально отказывается работать без ЯВНОГО
    DATABASE_URL — иначе случайный прогон миграций уйдёт в боевую базу.
    Правило верное и остаётся; следствие в том, что поведенческий тест,
    решивший работать с TEST_DATABASE_URL, обязан сам направить туда же
    Alembic — иначе он падает с SystemExit вместо честного skip.

    Найдено первым прогоном CI с живым Postgres: три теста уходили в skip
    локально (переменной нет) и падали в CI (переменная есть, но Alembic
    читает другое имя). Без живой базы расхождение двух имён было невидимо.
    """
    from cryptography.fernet import Fernet

    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("нет TEST_DATABASE_URL с живым Postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    # scripts/migrate_sqlite_to_pg.py отказывается переносить секреты
    # открытым текстом без APP_ENCRYPTION_KEY — правило верное, и тест,
    # запускающий перенос, обязан дать ключ так же, как это делает оператор.
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-app")
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def second_client(app, db_engine):
    """Второй «браузерный» клиент — для проверок изоляции тенантов.

    raw_client для этого не годится: он намеренно не шлёт csrf-заголовок
    (его задача — негативные проверки 2.6), поэтому любой не-GET от него
    возвращает 403 и тест изоляции проверяет CSRF вместо изоляции.
    Появился при переносе роутера каналов (К2), где нужны два
    одновременно авторизованных пользователя.
    """
    app.dependency_overrides[get_db] = _override_get_db(db_engine)
    transport = ASGITransport(app=app)
    async with FrontendLikeClient(transport=transport, base_url="http://test") as ac:
        await ac.get("/health")
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch, request):
    """Тесты не ходят в интернет.

    Найдено при переносе каталога моделей (К2): свип изоляции дёрнул
    /api/openrouter/models, и прогон ушёл в настоящий OpenRouter — 427
    моделей в ответе. Сетевой вызов делает тест медленным, ненадёжным и
    зависящим от чужого сервиса, а в CI ещё и утекающим наружу.

    Разрешить осознанно: пометить тест `@pytest.mark.allow_network`.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    import httpx

    async def blocked(self, *a, **kw):
        raise RuntimeError(
            "исходящий HTTP-запрос из теста заблокирован: подмените зависимость "
            "(get_model_lister / get_outbound / get_entity_resolver) или пометьте "
            "тест @pytest.mark.allow_network"
        )

    # Именно AsyncHTTPTransport — это настоящая сеть. Перехватывать
    # AsyncClient.send нельзя: через него же идёт ASGITransport, которым
    # тестовые клиенты обращаются к приложению в памяти.
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", blocked, raising=True
    )
