"""Задача 2.4 — регистрация, вход, выход.

Перечисление пользователей через signup должно быть невозможно: ответ
«email занят» не отличается от прочих ошибок валидации настолько, чтобы
по нему перебирать адреса. Вход с неверным паролем — единый 401 для
«нет такого юзера» и «пароль не тот» (то же правило).
"""
import pytest

from app.security.sessions import SESSION_COOKIE

CRED = {
    "email": "new@example.com",
    "password": "long-enough-password",
    "timezone": "Europe/Moscow",
}


@pytest.mark.asyncio
async def test_signup_creates_user_and_session(anon_client):
    r = await anon_client.post("/auth/signup", json=CRED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == CRED["email"]
    # пароль (ни в каком виде) наружу не отдаётся
    assert "password" not in body and "password_hash" not in body
    # сессия уже в cookie клиента — /auth/me отдаёт того же пользователя
    r2 = await anon_client.get("/auth/me")
    assert r2.status_code == 200
    assert r2.json()["email"] == CRED["email"]
    assert r2.json()["timezone"] == CRED["timezone"]


@pytest.mark.asyncio
async def test_duplicate_email_422_indistinct(anon_client):
    first = await anon_client.post("/auth/signup", json=CRED)
    assert first.status_code == 200
    r = await anon_client.post("/auth/signup", json=CRED)
    assert r.status_code == 422, r.text
    # формулировка не выдаёт, что именно email занят (иначе — перечислитель)
    text = r.text.lower()
    assert "занят" not in text and "существует" not in text and "exists" not in text


@pytest.mark.asyncio
async def test_short_password_422(anon_client):
    r = await anon_client.post(
        "/auth/signup", json={**CRED, "email": "other@example.com", "password": "short"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_over72byte_password_422(anon_client):
    # bcrypt молча усекает до 72 байт — хеширование откажется, эндпоинт тоже
    r = await anon_client.post(
        "/auth/signup", json={**CRED, "email": "other@example.com", "password": "x" * 73}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_me_logout_cycle(anon_client):
    await anon_client.post("/auth/signup", json=CRED)
    # выходим из сессии, не трогая csrf-cookie (иначе 2.6 сломает прайминг)
    anon_client.cookies.delete(SESSION_COOKIE)
    assert (await anon_client.get("/auth/me")).status_code == 401

    r = await anon_client.post(
        "/auth/login", json={"email": CRED["email"], "password": CRED["password"]}
    )
    assert r.status_code == 200, r.text
    r2 = await anon_client.get("/auth/me")
    assert r2.status_code == 200
    assert r2.json()["email"] == CRED["email"]

    r3 = await anon_client.post("/auth/logout")
    assert r3.status_code == 200
    r4 = await anon_client.get("/auth/me")
    assert r4.status_code == 401, "после логаута та же cookie обязана быть мертва"


@pytest.mark.asyncio
async def test_wrong_password_unified_401(anon_client):
    await anon_client.post("/auth/signup", json=CRED)
    anon_client.cookies.delete(SESSION_COOKIE)
    r = await anon_client.post(
        "/auth/login", json={"email": CRED["email"], "password": "wrong-password"}
    )
    assert r.status_code == 401
    # и несуществующий email — тот же ответ, без разницы формулировки
    r2 = await anon_client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "whatever-long"}
    )
    assert r2.status_code == 401
    assert r.json() == r2.json(), (
        "ответы «нет юзера» и «не тот пароль» обязаны совпадать побайтово — иначе перечисление"
    )