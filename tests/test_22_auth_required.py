"""Задача 2.3 — закрыто по умолчанию.

Порядок из Rails-шаблона: require_authentication висит на всём приложении,
публичные экшены — явный opt-out. Забыть закрыть эндпоинт невозможно, забыть
открыть — заметно сразу. Тест перебирает ВСЕ маршруты приложения: любой новый
роутер без Depends(require_user) падает здесь автоматически, вручную
перечислять защищённые пути не нужно.

Поправка к псевдокоду плана: проверка `path.startswith(p)` при `p == "/"`
истинна для ЛЮБОГО пути (все пути начинаются с "/") — белый список глотал бы
всё приложение, и тест вырождался в вакуумный. "/" и "/static" сверяются
отдельно: первый — точным равенством, второй — префиксом.
"""
import pytest

from app.security.sessions import SESSION_COOKIE, create_session, sign_session_id

# opt-out: единственные пути, куда анониму можно. Всё остальное — 401.
PUBLIC_PREFIXES = {
    "/health",
    "/auth/login",
    "/auth/signup",
    "/auth/password-reset",
    "/auth/google",
}
PUBLIC_EXACT = {"/", "/static"}


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _walk(routes):
    """FastAPI 0.141 оборачивает include_router в _IncludedRouter без .path:
    реальные маршруты лежат в original_router.routes — разворачиваем рекурсивно."""
    for route in routes:
        sub = getattr(route, "original_router", None)
        if sub is not None:
            yield from _walk(sub.routes)
        else:
            yield route


@pytest.mark.asyncio
async def test_every_route_requires_auth_unless_whitelisted(anon_client):
    from app.main import app

    checked = []
    for route in _walk(app.routes):
        path = getattr(route, "path", None)
        if not path or _is_public(path):
            continue
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            r = await anon_client.request(
                method, path.replace("{id}", "1").replace("{monitor_id}", "1")
            )
            checked.append(f"{method} {path}")
            assert r.status_code == 401, (
                f"{method} {path} доступен анониму ({r.status_code})"
            )
    # защита от вакуума: если защищённых маршрутов нет, тест ничего не проверяет
    assert checked, "ни одного защищённого маршрута — тест вырожден"


def test_non_public_routes_carry_router_level_require_user():
    """Суть 2.3 — закрытие на уровне РОУТЕРА, а не памяти разработчика:
    новый эндпоинт в защищённом роутере обязан получить require_user
    автоматически. Поведенческий тест выше видит только уже написанные
    эндпоинты; этот ловит «добавили роутер без dependencies=[...]»."""
    from app.deps import require_user
    from app.main import app

    found = 0
    for route in _walk(app.routes):
        path = getattr(route, "path", None)
        if not path or _is_public(path):
            continue
        deps = [d.dependency for d in getattr(route, "dependencies", [])]
        assert require_user in deps, (
            f"{path} не несёт Depends(require_user) от роутера — "
            "закрытие не «по умолчанию»"
        )
        found += 1
    assert found, "тест вырожден: защищённых маршрутов нет"


@pytest.mark.asyncio
async def test_health_is_public_and_leaks_nothing(anon_client):
    r = await anon_client.get("/health")
    assert r.status_code == 200
    # только статус: id/username аккаунта из /health убраны (см. трекер, PLAN.md раздел 12)
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_valid_session_passes_require_user(anon_client, db, user):
    """Зависимость не только отсекает анонимов, но и пропускает живую сессию."""
    session = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    anon_client.cookies.set(SESSION_COOKIE, sign_session_id(session.id))
    r = await anon_client.get("/auth/me")
    assert r.status_code == 200, f"валидная сессия не прошла: {r.status_code}"
    assert r.json()["email"] == user.email