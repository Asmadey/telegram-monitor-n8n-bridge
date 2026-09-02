"""Задача 3.3 — auth_state в БД вместо глобальной переменной.

server.py:39 — модульный словарь auth_state: два одновременных входа
перетирают друг друга, в мульти-тенанте это прямая дыра — B завершает
вход, начатый A, и получает его аккаунт. Здесь state живёт в
tg_auth_attempts по user_id (TTL 10 минут), sign-in берёт
phone_code_hash СТРОКОЙ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ — из тела его принять
нельзя (иначе таблица не помогает).

Telethon-клиент подменён фейком: тесты не ходят в живой Telegram.
"""

import datetime
import types

import pytest
from sqlalchemy import text

from app.security.crypto import decrypt
from app.security.csrf import CSRF_COOKIE, make_csrf_token
from app.security.sessions import SESSION_COOKIE, create_session, sign_session_id

FAKE_SESSION = "1BQANOTE-fake-mtproto-session"
PHONE_A = "+70000000001"
PHONE_B = "+70000000002"


class FakeTelegramClient:
    """Минимальная поверхность Telethon, нужная потоку входа.
    Записывает вызовы sign_in — тест проверяет, ЧЬЙ hash был использован."""

    def __init__(self):
        self.sign_in_calls: list[dict] = []
        self.authorized = False
        self.session = types.SimpleNamespace(save=lambda: FAKE_SESSION)

    async def send_code_request(self, phone: str):
        return types.SimpleNamespace(phone_code_hash=f"pch-{phone}")

    async def sign_in(
        self, *, phone=None, code=None, phone_code_hash=None, password=None
    ):
        self.sign_in_calls.append(
            {
                "phone": phone,
                "code": code,
                "phone_code_hash": phone_code_hash,
                "password": password,
            }
        )
        self.authorized = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        return types.SimpleNamespace(id=777, first_name="Tg", username="tester")

    async def connect(self):
        pass

    async def disconnect(self):
        pass


@pytest.fixture
def fake_tg(anon_client):
    """Подмена Telethon-клиента на уровне зависимости FastAPI."""
    from app.main import app
    from app.services.tg_auth import get_telegram_auth_client

    fake = FakeTelegramClient()

    async def _override():
        return fake

    app.dependency_overrides[get_telegram_auth_client] = _override
    yield fake
    app.dependency_overrides.pop(get_telegram_auth_client, None)


async def _act_as(anon_client, db, user) -> None:
    """Клиент действует от имени юзера: свежая сессия в cookie.

    Сессия создаётся прямо в БД (минуя login), поэтому csrf-токен
    выдаём так же, как его выдаёт _open_session при логине:
    ПОДПИСАННЫЙ и привязанный к sid (анон-токен с живой cookie сессии
    не проходит — так устроен verify_csrf, задача 2.6). Прежний
    csrf-cookie (домен test от прайминга) вычищаем из jar — httpx
    падает CookieConflict при двух cookie с одним именем."""
    session = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    for c in list(anon_client.cookies.jar):
        if c.name in (CSRF_COOKIE, SESSION_COOKIE):
            anon_client.cookies.jar.clear(c.domain, c.path, c.name)
    # httpx нормализует base_url http://test в хост test.local — cookie,
    # поставленная на домен "test", на запрос просто не уедет (403 CSRF).
    anon_client.cookies.set(
        SESSION_COOKIE, sign_session_id(session.id), domain="test.local", path="/"
    )
    anon_client.cookies.set(
        CSRF_COOKIE, make_csrf_token(session.id), domain="test.local", path="/"
    )


async def _attempt_rows(db, user_id: int) -> list:
    result = await db.execute(
        text("SELECT phone, phone_code_hash FROM tg_auth_attempts WHERE user_id = :u"),
        {"u": user_id},
    )
    return list(result)


@pytest.mark.asyncio
async def test_parallel_send_codes_do_not_interfere(
    anon_client, db, user_a, user_b, fake_tg
):
    """Два send-code от разных юзеров: у каждого СВОЯ строка в БД со СВОИМ
    hash. Глобальный словарь (server.py) перетирал бы первую попытку."""
    await _act_as(anon_client, db, user_a)
    r_a = await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
    assert r_a.status_code == 200, r_a.text
    # ответ не выдаёт phone_code_hash: с ним вход можно завершить минуя БД
    assert "phone_code_hash" not in r_a.text

    await _act_as(anon_client, db, user_b)
    r_b = await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_B})
    assert r_b.status_code == 200, r_b.text

    rows_a = await _attempt_rows(db, user_a.id)
    rows_b = await _attempt_rows(db, user_b.id)
    assert rows_a == [(PHONE_A, f"pch-{PHONE_A}")], (
        f"попытка A потеряна/искажена после send-code B: {rows_a}"
    )
    assert rows_b == [(PHONE_B, f"pch-{PHONE_B}")], f"попытка B не записана: {rows_b}"

    # повторный запрос кода отменяет СТАРУЮ попытку того же юзера
    await _act_as(anon_client, db, user_a)
    await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
    assert len(await _attempt_rows(db, user_a.id)) == 1, (
        "у юзера больше одной активной попытки"
    )


@pytest.mark.asyncio
async def test_sign_in_takes_hash_from_own_row_not_body(
    anon_client, db, user_a, user_b, fake_tg
):
    """B не может использовать hash A: без своей строки — 400; со своей —
    вход идёт по СТРОКЕ B, даже если тело несёт hash A."""
    await _act_as(anon_client, db, user_a)
    await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})

    # B без своей попытки, в теле — чужой hash: вход невозможен
    await _act_as(anon_client, db, user_b)
    r = await anon_client.post(
        "/api/telegram/sign-in",
        json={"code": "11111", "phone_code_hash": f"pch-{PHONE_A}"},
    )
    assert r.status_code == 400, "sign-in без своей попытки прошёл"
    assert fake_tg.sign_in_calls == [], (
        "клиент Telegram был вызван без валидной попытки"
    )

    # B со своей попыткой: hash берётся ИЗ СТРОКИ, тело с чужим hash игнорируется
    await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_B})
    r = await anon_client.post(
        "/api/telegram/sign-in",
        json={"code": "11111", "phone_code_hash": f"pch-{PHONE_A}"},
    )
    assert r.status_code == 200, r.text
    assert fake_tg.sign_in_calls[0]["phone_code_hash"] == f"pch-{PHONE_B}", (
        "sign-in использовал phone_code_hash из тела, а не из строки юзера"
    )


@pytest.mark.asyncio
async def test_sign_in_stores_encrypted_session_and_consumes_attempt(
    anon_client, db, user_a, fake_tg
):
    """Успешный вход сохраняет MTProto-сессию зашифрованной (задача 3.2)
    и съедает попытку: повторный sign-in по той же строке невозможен."""
    await _act_as(anon_client, db, user_a)
    await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
    r = await anon_client.post("/api/telegram/sign-in", json={"code": "11111"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "authorized"

    raw = (
        await db.execute(
            text(
                "SELECT session_string_encrypted, phone, tg_user_id "
                "FROM telegram_accounts WHERE user_id = :u"
            ),
            {"u": user_a.id},
        )
    ).first()
    assert raw, "telegram_accounts не заполнена после входа"
    assert FAKE_SESSION not in raw[0], "сессия сохранена открытым текстом"
    assert decrypt(raw[0]) == FAKE_SESSION
    assert raw[1] == PHONE_A and raw[2] == 777, "phone/tg_user_id не заполнены"

    assert await _attempt_rows(db, user_a.id) == [], "попытка не съедена после входа"


@pytest.mark.asyncio
async def test_expired_attempt_rejected(anon_client, db, user_a, fake_tg):
    """TTL 10 минут: истёкшая попытка не работает."""
    await _act_as(anon_client, db, user_a)
    await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    await db.execute(
        text("UPDATE tg_auth_attempts SET expires_at = :e WHERE user_id = :u"),
        {"e": past, "u": user_a.id},
    )
    await db.commit()
    r = await anon_client.post("/api/telegram/sign-in", json={"code": "11111"})
    assert r.status_code == 400, "истёкшая попытка прошла"
    assert fake_tg.sign_in_calls == []


@pytest.mark.asyncio
async def test_send_code_rate_limit_3_per_hour(anon_client, db, user_a, fake_tg):
    """3/час (задача 2.7): защита аккаунта КЛИЕНТА — Telegram отвечает на
    спам кодов FloodWaitError и может ограничить аккаунт на дни."""
    await _act_as(anon_client, db, user_a)
    for i in range(3):
        r = await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
        assert r.status_code == 200, f"send-code №{i + 1} не прошёл: {r.status_code}"
    r4 = await anon_client.post("/api/telegram/send-code", json={"phone": PHONE_A})
    assert r4.status_code == 429, "4-й запрос кода за час не отсечён"


def test_tg_auth_attempt_ttl_is_10_minutes():
    """TTL попытки — 10 минут (план 3.3)."""
    from app.api.telegram import ATTEMPT_TTL

    assert ATTEMPT_TTL == datetime.timedelta(minutes=10)
