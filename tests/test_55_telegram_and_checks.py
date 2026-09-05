"""К2 — остаток монолита: диалоги, выход из Telegram и проверки интеграций.

`GET /dialogs` (server.py:1233) в монолите открыт: он отдаёт список ВСЕХ чатов
и переписок владельца аккаунта — самый чувствительный эндпоинт из всех.

Проверочные кнопки («тестовый вебхук», «проверить ключ», «тестовое
сообщение») в оригинале ходят наружу прямо из обработчика. Здесь исходящий
вызов — инъектируемая зависимость: тест не ходит в сеть, а вебхук перед
отправкой проходит ту же проверку SSRF, что и при сохранении, — иначе
«проверить» превращается в готовый сканер внутренней сети.

Выход из Telegram обязан стирать сохранённую MTProto-сессию: оставить её
после «выйти» — значит оставить доступ к аккаунту, который пользователь
считает отключённым.
"""

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import TelegramAccount

pytestmark = pytest.mark.asyncio


class FakeDialog:
    def __init__(self, name, entity_id, username=None):
        self.name = name
        self.id = entity_id
        self.entity = type("E", (), {"username": username})()
        self.is_channel, self.is_group, self.is_user = True, False, False


@pytest.fixture
def fake_dialogs(app):
    from app.api.telegram import get_dialog_lister

    async def lister(limit: int = 50):
        return [FakeDialog("Канал", -100500, "channel0")]

    app.dependency_overrides[get_dialog_lister] = lambda: lister
    yield
    app.dependency_overrides.pop(get_dialog_lister, None)


@pytest.fixture
def fake_sender(app):
    """Исходящие проверки не ходят в сеть; запись в sent видна тесту."""
    from app.api.checks import get_outbound

    sent: list[tuple[str, str]] = []

    async def outbound(kind: str, target: str, payload=None):
        if "internal" in target:
            from app.services.webhook import UnsafeWebhookURL

            raise UnsafeWebhookURL(target, "тестовая заглушка")
        sent.append((kind, target))
        return {"ok": True}

    app.dependency_overrides[get_outbound] = lambda: outbound
    yield sent
    app.dependency_overrides.pop(get_outbound, None)


# --------------------------------------------------------------------------
# Диалоги
# --------------------------------------------------------------------------


async def test_dialogs_require_auth_and_return_own_chats(
    anon_client, db, user, fake_dialogs
):
    await act_as(anon_client, db, user)
    resp = await anon_client.get("/api/telegram/dialogs")
    assert resp.status_code == 200, resp.text
    dialogs = resp.json()["dialogs"]
    assert dialogs[0]["name"] == "Канал"
    assert dialogs[0]["username"] == "channel0"


async def test_dialogs_are_closed_to_anonymous(anon_client, fake_dialogs):
    """Список всех чатов владельца — самый чувствительный эндпоинт монолита,
    и там он был открыт всему интернету."""
    assert (await anon_client.get("/api/telegram/dialogs")).status_code == 401


# --------------------------------------------------------------------------
# Выход из Telegram
# --------------------------------------------------------------------------


async def test_logout_erases_the_stored_session(anon_client, db, user):
    db.add(
        TelegramAccount(
            user_id=user.id,
            phone="+70000000000",
            session_string_encrypted="зашифровано",
            tg_user_id=1,
        )
    )
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.post("/api/telegram/logout")
    assert resp.status_code == 200, resp.text

    left = await db.scalar(
        select(func.count())
        .select_from(TelegramAccount)
        .where(TelegramAccount.user_id == user.id)
    )
    assert left == 0, "сессия осталась: доступ к аккаунту не отозван"


async def test_logout_does_not_touch_other_tenants(anon_client, db, user_a, user_b):
    for owner in (user_a, user_b):
        db.add(
            TelegramAccount(
                user_id=owner.id,
                phone="+70000000000",
                session_string_encrypted="зашифровано",
                tg_user_id=owner.id,
            )
        )
    await db.commit()
    await act_as(anon_client, db, user_a)

    await anon_client.post("/api/telegram/logout")
    left_b = await db.scalar(
        select(func.count())
        .select_from(TelegramAccount)
        .where(TelegramAccount.user_id == user_b.id)
    )
    assert left_b == 1, "выход одного пользователя отключил другого"


# --------------------------------------------------------------------------
# Проверки интеграций
# --------------------------------------------------------------------------


async def test_webhook_test_refuses_unsafe_address(anon_client, db, user, fake_sender):
    """«Проверить вебхук» не должно превращаться в сканер внутренней сети."""
    from app.services.integrations import save_integration_secrets

    await save_integration_secrets(
        db, user.id, webhook_url="http://internal.service/hook"
    )
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.post("/api/webhook/test")
    assert resp.status_code == 400, resp.text
    assert fake_sender == [], "запрос ушёл на небезопасный адрес"


async def test_webhook_test_sends_when_configured(anon_client, db, user, fake_sender):
    from app.services.integrations import save_integration_secrets

    await save_integration_secrets(
        db, user.id, webhook_url="https://n8n.example.com/hook"
    )
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.post("/api/webhook/test")
    assert resp.status_code == 200, resp.text
    assert [kind for kind, _ in fake_sender] == ["webhook"]


async def test_check_without_configuration_is_400_not_500(
    anon_client, db, user, fake_sender
):
    """Ничего не настроено — понятный отказ, а не исключение в обработчике."""
    await act_as(anon_client, db, user)
    for path in (
        "/api/webhook/test",
        "/api/openrouter/test",
        "/api/telegram-forward/test",
    ):
        resp = await anon_client.post(path)
        assert resp.status_code == 400, f"{path} → {resp.status_code}"
