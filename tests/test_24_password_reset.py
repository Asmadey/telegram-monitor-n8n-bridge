"""Задача 2.5 — сброс пароля.

Два критичных свойства:
1. Ответ POST /auth/password-reset ОДИНАКОВ для существующего и
   несуществующего email (побайтово) — иначе эндпоинт перечисляет юзеров.
2. Токен одноразовый: в подпись включён слепок password_hash; после смены
   пароля хеш меняется — старый токен расходится (приём Rails
   generates_token_for). Плюс часовой max_age.
"""

import time

import pytest
from itsdangerous.timed import TimestampSigner

from app.models import User
from app.security.password_reset import make_reset_token
from app.security.passwords import hash_password
from app.security.sessions import SESSION_COOKIE

OLD_PASSWORD = "old-password-long"
NEW_PASSWORD = "new-password-long"
RESET_TTL_SECONDS = 3600  # план 2.5: час


async def _make_user_with_password(db) -> User:
    u = User(email="reset@example.com", password_hash=hash_password(OLD_PASSWORD))
    db.add(u)
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_reset_response_identical_for_known_and_unknown_email(anon_client, db):
    await _make_user_with_password(db)
    a = await anon_client.post(
        "/auth/password-reset", json={"email": "reset@example.com"}
    )
    b = await anon_client.post(
        "/auth/password-reset", json={"email": "nobody@example.com"}
    )
    assert a.status_code == b.status_code, (
        f"статусы различаются: {a.status_code} != {b.status_code} — перечислитель"
    )
    assert a.json() == b.json(), (
        "тела ответов различаются — по ним можно перебирать адреса"
    )


@pytest.mark.asyncio
async def test_confirm_changes_password_and_kills_sessions(anon_client, db):
    user = await _make_user_with_password(db)
    # живая сессия ДО смены пароля
    token = make_reset_token(user)
    # залогинимся честно — сессия в cookie клиента
    r = await anon_client.post(
        "/auth/login", json={"email": "reset@example.com", "password": OLD_PASSWORD}
    )
    assert r.status_code == 200

    r = await anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200, r.text
    # все прежние сессии мертвы — смену пароля переживать они не должны
    assert (await anon_client.get("/auth/me")).status_code == 401
    # старый пароль больше не работает, новый — работает
    anon_client.cookies.delete(SESSION_COOKIE)
    r_old = await anon_client.post(
        "/auth/login", json={"email": "reset@example.com", "password": OLD_PASSWORD}
    )
    assert r_old.status_code == 401
    r_new = await anon_client.post(
        "/auth/login", json={"email": "reset@example.com", "password": NEW_PASSWORD}
    )
    assert r_new.status_code == 200


@pytest.mark.asyncio
async def test_token_single_use(anon_client, db):
    user = await _make_user_with_password(db)
    token = make_reset_token(user)
    first = await anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert first.status_code == 200
    second = await anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-password-1"},
    )
    assert second.status_code == 422, "тот же токен сработал второй раз"


@pytest.mark.asyncio
async def test_token_expires_after_one_hour(anon_client, db, monkeypatch):
    user = await _make_user_with_password(db)
    # токен «из прошлого»: get_timestamp отдаёт эпоху на 2 часа старше.
    # Патч снимается ДО проверки — иначе age считался бы как
    # (now-7200) - (signed-7200) ≈ 0, и «старый» токен выходил свежим
    # (ошибка теста, не реализации; поймана ручным воспроизведением).
    with monkeypatch.context() as m:
        m.setattr(
            TimestampSigner,
            "get_timestamp",
            lambda self: int(time.time()) - 2 * RESET_TTL_SECONDS,
        )
        stale = make_reset_token(user)
    r = await anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": stale, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 422, "токен старше часа прошёл"


@pytest.mark.asyncio
async def test_tampered_token_rejected(anon_client, db):
    await _make_user_with_password(db)
    r = await anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": "garbage.token", "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_weak_new_password_rejected(anon_client, db):
    user = await _make_user_with_password(db)
    token = make_reset_token(user)
    r = await anon_client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_reset_email_only_for_known_user(anon_client, db, tmp_path):
    """Письмо (в dev — файл-аутбокс, задача 2.9) создаётся только
    существующему адресу; несуществующему — ничего не пишется."""
    from app.security.password_reset import RESET_TTL

    assert RESET_TTL == RESET_TTL_SECONDS, "TTL сброса — час (план 2.5)"
    mail_dir = tmp_path / "mail"  # тот же tmp_path, что и в _env (conftest)
    await _make_user_with_password(db)
    await anon_client.post("/auth/password-reset", json={"email": "reset@example.com"})
    files = list(mail_dir.glob("*.html"))
    assert len(files) == 1, "письмо сброса не ушло существующему юзеру"
    assert "reset@example.com" in files[0].read_text(encoding="utf-8")

    await anon_client.post("/auth/password-reset", json={"email": "ghost@example.com"})
    assert len(list(mail_dir.glob("*.html"))) == 1, "письмо ушло несуществующему адресу"
