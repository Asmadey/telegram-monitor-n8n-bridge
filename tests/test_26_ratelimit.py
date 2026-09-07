"""Задача 2.7 — rate limiting.

Брутфорс (login/signup — 10/3 мин на IP), спам письмами (password-reset —
5/час на пару IP+email), запрос кода Telegram — 3/час на пользователя:
это не «защита сервера», а защита аккаунта КЛИЕНТА (Telegram отвечает на
спам FloodWaitError и может ограничить аккаунт на дни). Эндпоинт
/api/telegram/send-code переносится из server.py в задаче 3.3 — там же
вешается лимит; здесь проверяем, что политика уже задекларирована.
"""

import pytest

from app.security.ratelimit import TELEGRAM_SEND_CODE_LIMIT

LOGIN = {"email": "brute@example.com", "password": "wrong-password-1"}


@pytest.mark.asyncio
async def test_login_bruitforce_429_on_11th(anon_client, db):
    for _ in range(10):
        r = await anon_client.post("/auth/login", json=LOGIN)
        assert r.status_code == 401  # лимит ещё не исчерпан — но входа нет
    r11 = await anon_client.post("/auth/login", json=LOGIN)
    assert r11.status_code == 429, "11-я попытка логина за 3 минуты не отсечена"


@pytest.mark.asyncio
async def test_signup_spam_429_on_11th(anon_client, db):
    # одна и та же почта: 1-я создаст юзера, остальные 422 «не отличимо» —
    # лимит считает попытки, не успехи
    for _ in range(10):
        r = await anon_client.post(
            "/auth/signup",
            json={"email": "spam@example.com", "password": "long-enough-password"},
        )
        assert r.status_code in (200, 422)
    r11 = await anon_client.post(
        "/auth/signup",
        json={"email": "spam@example.com", "password": "long-enough-password"},
    )
    assert r11.status_code == 429, "11-я регистрация за 3 минуты не отсечена"


@pytest.mark.asyncio
async def test_password_reset_429_on_6th_same_email(anon_client, db):
    """5/час на пару IP+email: жертва получает максимум 5 писем в час,
    сколько бы адресов спамер ни перебирал с одного IP."""
    for _ in range(5):
        r = await anon_client.post(
            "/auth/password-reset", json={"email": "victim@example.com"}
        )
        assert r.status_code == 200
    r6 = await anon_client.post(
        "/auth/password-reset", json={"email": "victim@example.com"}
    )
    assert r6.status_code == 429, "6-й сброс пароля на тот же email не отсечён"


def test_telegram_send_code_policy_declared():
    """Пока эндпоинт в server.py (перенос — задача 3.3), политика зафиксирована."""
    assert TELEGRAM_SEND_CODE_LIMIT == "3/hour", (
        "лимит запроса кода Telegram — 3/час (план 2.7: защита аккаунта клиента)"
    )
