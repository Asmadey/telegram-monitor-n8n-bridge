"""Задача 2.8 — админка.

Порт admin/base_controller.rb: require_admin как зависимость роутера
/api/admin/*. Обычный пользователь — 403, аноним — 401, админ — 200.
403 для не-админа здесь легален (админка — свой ресурс, существование
не секрет); правило «на чужих ресурсах 404» — про данные тенантов,
не про роль.
"""

import pytest

from app.models import User
from app.security.passwords import hash_password


async def _login(anon_client, email: str, password: str) -> None:
    r = await anon_client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, f"не залогинился {email}: {r.text}"


@pytest.mark.asyncio
async def test_anonymous_gets_401_on_admin(db, anon_client):
    r = await anon_client.get("/api/admin/users")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_regular_user_gets_403_on_admin(db, anon_client):
    u = User(email="plain@example.com", password_hash=hash_password("plain-password-1"))
    db.add(u)
    await db.commit()
    await _login(anon_client, "plain@example.com", "plain-password-1")
    assert (await anon_client.get("/auth/me")).status_code == 200
    r = await anon_client.get("/api/admin/users")
    assert r.status_code == 403, "не-админ прошёл в админку"
    r2 = await anon_client.get("/api/admin/users/1")
    assert r2.status_code == 403, "не-админ прошёл в конкретного юзера"


@pytest.mark.asyncio
async def test_admin_lists_and_reads_users(db, anon_client):
    admin = User(
        email="admin@example.com",
        password_hash=hash_password("admin-password-1"),
        is_admin=True,
    )
    plain = User(
        email="plain@example.com",
        password_hash=hash_password("plain-password-1"),
    )
    db.add_all([admin, plain])
    await db.commit()

    await _login(anon_client, "admin@example.com", "admin-password-1")
    r = await anon_client.get("/api/admin/users")
    assert r.status_code == 200, r.text
    emails = [row["email"] for row in r.json()]
    assert emails == ["admin@example.com", "plain@example.com"]
    # хеши паролей наружу не отдаются даже админу
    for row in r.json():
        assert "password_hash" not in row and "password" not in row

    r2 = await anon_client.get(f"/api/admin/users/{plain.id}")
    assert r2.status_code == 200
    assert r2.json()["email"] == "plain@example.com"
    assert "password_hash" not in r2.json()

    # несуществующий — 404 (админ уже знает, что юзеры бывают)
    r3 = await anon_client.get("/api/admin/users/99999")
    assert r3.status_code == 404
