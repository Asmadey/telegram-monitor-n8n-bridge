"""К2 — настройки интеграций (server.py:1530-1791): n8n, OpenRouter, бот.

Самая опасная часть монолита. Там эти эндпоинты открыты всему интернету и
отдают сырые ключи (К3, закрыто в 0.3), хранят их открытым текстом (К4) и
принимают произвольный webhook_url без проверки (К5). Здесь всё это уже
закрыто сервисным слоем — задача роутера не растерять гарантии:

- наружу уходит только маска и признак наличия;
- поле, которого нет в запросе, НЕ затирает сохранённый ключ (иначе
  сохранение формы вытирает секрет, С16/0.3), а пустая строка — затирает
  осознанно;
- webhook проверяется на SSRF ДО записи: сохранить адрес во внутреннюю
  сеть и отправлять туда по расписанию — та же дыра, просто отложенная.
"""

import pytest
from conftest import act_as

from app.models import Integration
from app.security.crypto import decrypt

pytestmark = pytest.mark.asyncio

GOOD_WEBHOOK = "https://n8n.example.com/webhook/abc"
OPENROUTER_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
BOT_TOKEN = "123456789:AAF-abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def allow_webhook(app):
    """Проверка SSRF не должна ходить в DNS из тестов."""
    from app.api.integrations import get_webhook_validator

    async def validator(url: str) -> None:
        if "internal" in url:
            from app.services.webhook import UnsafeWebhookURL

            raise UnsafeWebhookURL(url, "тестовая заглушка: внутренний адрес")

    app.dependency_overrides[get_webhook_validator] = lambda: validator
    yield
    app.dependency_overrides.pop(get_webhook_validator, None)


async def _row(db, user) -> Integration:
    from sqlalchemy import select

    return (
        await db.scalars(select(Integration).where(Integration.user_id == user.id))
    ).first()


# --------------------------------------------------------------------------
# Секреты наружу не уходят
# --------------------------------------------------------------------------


async def test_saved_secrets_come_back_only_masked(
    anon_client, db, user, allow_webhook
):
    await act_as(anon_client, db, user)
    await anon_client.post(
        "/api/openrouter", json={"api_key": OPENROUTER_KEY, "is_enabled": True}
    )

    body = (await anon_client.get("/api/openrouter")).json()
    assert body["has_key"] is True
    assert OPENROUTER_KEY not in str(body), "сырой ключ ушёл наружу"
    assert body["api_key_masked"].endswith(OPENROUTER_KEY[-4:])
    assert "api_key" not in body


async def test_secrets_are_encrypted_at_rest(anon_client, db, user, allow_webhook):
    await act_as(anon_client, db, user)
    await anon_client.post("/api/telegram-forward", json={"bot_token": BOT_TOKEN})
    await db.commit()

    row = await _row(db, user)
    await db.refresh(row)
    assert BOT_TOKEN not in row.telegram_bot_token_encrypted, "токен лежит открытым"
    assert decrypt(row.telegram_bot_token_encrypted) == BOT_TOKEN


# --------------------------------------------------------------------------
# Контракт «не затирать»
# --------------------------------------------------------------------------


async def test_saving_form_without_secret_keeps_it(
    anon_client, db, user, allow_webhook
):
    """Форма сохраняется с пустым полем ключа (там маска-плейсхолдер) —
    сохранённый ключ обязан уцелеть."""
    await act_as(anon_client, db, user)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})

    await anon_client.post(
        "/api/openrouter", json={"model": "deepseek/deepseek-v4-flash"}
    )
    assert (await anon_client.get("/api/openrouter")).json()["has_key"] is True


async def test_empty_string_clears_the_secret(anon_client, db, user, allow_webhook):
    """Явная пустая строка — осознанная очистка, а не «не передано»."""
    await act_as(anon_client, db, user)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})

    await anon_client.post("/api/openrouter", json={"api_key": ""})
    assert (await anon_client.get("/api/openrouter")).json()["has_key"] is False


# --------------------------------------------------------------------------
# Webhook и SSRF
# --------------------------------------------------------------------------


async def test_webhook_saved_and_masked(anon_client, db, user, allow_webhook):
    await act_as(anon_client, db, user)
    saved = await anon_client.post(
        "/api/webhook", json={"webhook_url": GOOD_WEBHOOK, "auto_webhook_enabled": True}
    )
    assert saved.status_code == 200, saved.text

    body = (await anon_client.get("/api/webhook")).json()
    assert body["has_webhook"] is True
    assert body["auto_webhook_enabled"] is True
    assert GOOD_WEBHOOK not in str(body), "адрес вебхука ушёл наружу целиком"


async def test_unsafe_webhook_is_rejected_before_storing(
    anon_client, db, user, allow_webhook
):
    """Адрес во внутреннюю сеть нельзя даже сохранить: отложенная отправка
    по расписанию — та же SSRF, просто позже."""
    await act_as(anon_client, db, user)
    bad = await anon_client.post(
        "/api/webhook", json={"webhook_url": "http://internal.service/hook"}
    )
    assert bad.status_code == 400, bad.text
    assert (await anon_client.get("/api/webhook")).json()["has_webhook"] is False


# --------------------------------------------------------------------------
# Изоляция
# --------------------------------------------------------------------------


async def test_integrations_are_per_tenant(
    anon_client, second_client, db, user_a, user_b, allow_webhook
):
    await act_as(anon_client, db, user_a)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})

    await act_as(second_client, db, user_b)
    assert (await second_client.get("/api/openrouter")).json()["has_key"] is False
