"""Задача 2.6 — CSRF: double-submit token.

Cookie-аутентификация без CSRF означает: сторонний сайт выполняет действие
от имени залогиненного пользователя. Double-submit: сервер выдаёт cookie
`csrf_token` (не HttpOnly — JS читает), фронтенд шлёт её значение в
`X-CSRF-Token`, middleware сверяет. Токен ПОДПИСАН и после логина привязан
к id сессии — подделанный cookie (взятый, например, с поддомена) не
пройдёт, даже если заголовок совпадает: сверка не строковая.
"""

import pytest

from app.security.csrf import CSRF_COOKIE, CSRF_HEADER, SAFE_METHODS
from tests.conftest import walk_routes

SIGNUP = {
    "email": "csrf@example.com",
    "password": "long-enough-password",
    "timezone": "UTC",
}


def _non_get_paths(app):
    for route in walk_routes(app.routes):
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = set(getattr(route, "methods", None) or set())
        for method in sorted(methods - SAFE_METHODS):
            yield method, path.replace("{id}", "1").replace("{monitor_id}", "1")


@pytest.mark.asyncio
async def test_every_non_get_route_rejects_missing_csrf(raw_client):
    """Перебор ВСЕХ не-GET маршрутов: забыть закрыть один нельзя."""
    from app.main import app

    checked = []
    for method, path in _non_get_paths(app):
        r = await raw_client.request(method, path)
        checked.append(f"{method} {path}")
        assert r.status_code == 403, (
            f"{method} {path} без CSRF-заголовка прошёл ({r.status_code})"
        )
    assert checked, "ни одного не-GET маршрута — тест вырожден"


@pytest.mark.asyncio
async def test_wrong_header_403(raw_client):
    await raw_client.get("/health")  # сервер выдал anon csrf-cookie
    r = await raw_client.post(
        "/auth/signup", json=SIGNUP, headers={CSRF_HEADER: "forged-token"}
    )
    assert r.status_code == 403, "несовпадающий заголовок прошёл CSRF"


@pytest.mark.asyncio
async def test_valid_header_passes(raw_client):
    await raw_client.get("/health")
    token = raw_client.cookies.get(CSRF_COOKIE)
    r = await raw_client.post("/auth/signup", json=SIGNUP, headers={CSRF_HEADER: token})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_forged_cookie_rejected_even_with_matching_header(raw_client):
    """Cookie-tossing: даже совпадающий заголовок не спасёт неподписанный cookie."""
    await raw_client.get("/health")
    raw_client.cookies.set(CSRF_COOKIE, "forged.value")
    r = await raw_client.post(
        "/auth/signup", json=SIGNUP, headers={CSRF_HEADER: "forged.value"}
    )
    assert r.status_code == 403, "сверка чисто строковая: подпись не проверяется"


@pytest.mark.asyncio
async def test_csrf_reissued_and_bound_to_session(raw_client):
    await raw_client.get("/health")
    anon_token = raw_client.cookies.get(CSRF_COOKIE)
    r = await raw_client.post(
        "/auth/signup", json=SIGNUP, headers={CSRF_HEADER: anon_token}
    )
    assert r.status_code == 200
    # логин/регистрация перевыпускают csrf, привязанный к sid новой сессии
    bound_token = raw_client.cookies.get(CSRF_COOKIE)
    assert bound_token != anon_token, "csrf не перевыпущен при создании сессии"

    # старый anon-токен в заголовке при новой cookie — не проходит
    r2 = await raw_client.post("/auth/logout", headers={CSRF_HEADER: anon_token})
    assert r2.status_code == 403, "анонимный csrf-токен сработал для сессии"

    # свежий привязанный — проходит
    r3 = await raw_client.post("/auth/logout", headers={CSRF_HEADER: bound_token})
    assert r3.status_code == 200, r3.text
