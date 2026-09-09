"""Поля интеграций показывают то, что лежит в базе (9.14).

Требование владельца 8 сентября: «инпуты должны всегда быть заполнены
текущими значениями; часть из них скрыта, такие как токены; по нажатию на
глазик они должны показываться».

Это правка контракта 0.3 (К3), и правка осознанная. Прежнее правило —
«сырой секрет не уходит наружу никогда» — родилось из монолита, где
`GET /api/openrouter` отдавал живой ключ **анониму**: аутентификации не
было вообще. Сегодня тот же ответ уходит только владельцу строки, за
входом, CSRF и мульти-тенантной изоляцией. Опасность была не в том, что
ключ показан хозяину, а в том, что он показан кому угодно.

Поэтому граница проводится заново, по смыслу поля:

- **адрес вебхука и ID получателя** — конфигурация, а не удостоверение.
  Их прячут зря: адрес нельзя проверить и нельзя починить, не видя его.
  Уходят владельцу целиком, в обычном ответе настроек.
- **ключ OpenRouter и токен бота** — удостоверения. В обычном ответе
  настроек их по-прежнему нет: этот ответ приходит при каждом открытии
  вкладки, и попадать в него секрету незачем. Показ — отдельное действие
  (`/reveal`), которое видно в журнале.

Отдельная запись в журнале здесь и есть цена показа: секрет, показанный
молча, отличить от секрета, украденного через угнанную сессию, нечем.
"""

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio

GOOD_WEBHOOK = "https://n8n.example.com/webhook/abc"
OPENROUTER_KEY = "sk-" + "or-v1-" + "fedcba9876543210" * 2
BOT_TOKEN = "9876" + "54321:" + "AAF-" + "zyxwvutsrqponmlkjihgfedcba543210"


@pytest.fixture
def allow_webhook(app):
    from app.api.integrations import get_webhook_validator

    async def validator(url: str) -> None:
        return None

    app.dependency_overrides[get_webhook_validator] = lambda: validator
    yield
    app.dependency_overrides.pop(get_webhook_validator, None)


async def _journal(anon_client) -> str:
    return (await anon_client.get("/api/logs?limit=50")).text


# --------------------------------------------------------------------------
# Конфигурация видна: адрес вебхука — не удостоверение
# --------------------------------------------------------------------------


async def test_webhook_address_comes_back_to_its_owner(
    anon_client, db, user, allow_webhook
):
    """Адрес, который нельзя увидеть, нельзя и починить.

    Маска здесь не защищала ничего: вебхук n8n задаёт сам пользователь и
    сам же отзывает его в n8n. Зато делала поле неработающим — открыв
    вкладку, владелец видел пустую строку и не мог отличить «адрес задан»
    от «адреса нет».
    """
    await act_as(anon_client, db, user)
    await anon_client.post("/api/webhook", json={"webhook_url": GOOD_WEBHOOK})

    body = (await anon_client.get("/api/webhook")).json()
    assert body["webhook_url"] == GOOD_WEBHOOK, (
        "адрес вебхука не вернулся владельцу — поле нечем заполнить"
    )
    assert body["has_webhook"] is True


async def test_webhook_address_stays_inside_its_tenant(
    anon_client, db, user, user_b, allow_webhook
):
    """Видимость не отменяет изоляцию: чужой адрес не виден никому."""
    await act_as(anon_client, db, user)
    await anon_client.post("/api/webhook", json={"webhook_url": GOOD_WEBHOOK})

    await act_as(anon_client, db, user_b)
    body = (await anon_client.get("/api/webhook")).json()
    assert body["webhook_url"] == "", "чужой адрес вебхука виден соседу"
    assert body["has_webhook"] is False


# --------------------------------------------------------------------------
# Удостоверения: показ — отдельное действие
# --------------------------------------------------------------------------


async def test_routine_settings_response_still_has_no_raw_secret(
    anon_client, db, user, allow_webhook
):
    """Обычный ответ настроек секрета не несёт — он ходит слишком часто."""
    await act_as(anon_client, db, user)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})
    await anon_client.post("/api/telegram-forward", json={"bot_token": BOT_TOKEN})

    assert OPENROUTER_KEY not in (await anon_client.get("/api/openrouter")).text
    assert BOT_TOKEN not in (await anon_client.get("/api/telegram-forward")).text


@pytest.mark.parametrize(
    "url, save, field",
    [
        ("/api/openrouter", {"api_key": OPENROUTER_KEY}, OPENROUTER_KEY),
        ("/api/telegram-forward", {"bot_token": BOT_TOKEN}, BOT_TOKEN),
    ],
    ids=["openrouter", "telegram-bot"],
)
async def test_owner_can_reveal_own_secret(
    anon_client, db, user, allow_webhook, url, save, field
):
    await act_as(anon_client, db, user)
    await anon_client.post(url, json=save)

    shown = await anon_client.post(f"{url}/reveal")
    assert shown.status_code == 200, shown.text
    assert shown.json()["secret"] == field, "владельцу не показали его же секрет"


@pytest.mark.parametrize(
    "url", ["/api/openrouter/reveal", "/api/telegram-forward/reveal"]
)
async def test_reveal_is_closed_to_anonymous(anon_client, url):
    assert (await anon_client.post(url)).status_code == 401


@pytest.mark.parametrize(
    "url", ["/api/openrouter/reveal", "/api/telegram-forward/reveal"]
)
async def test_reveal_of_a_missing_secret_is_empty_not_a_crash(
    anon_client, db, user, url
):
    """У нового пользователя строки настроек ещё нет вовсе."""
    await act_as(anon_client, db, user)
    shown = await anon_client.post(url)
    assert shown.status_code == 200, shown.text
    assert shown.json()["secret"] == ""


async def test_reveal_leaves_a_trace_in_the_journal(
    anon_client, db, user, allow_webhook
):
    """Молчаливый показ секрета неотличим от кражи через чужую сессию."""
    await act_as(anon_client, db, user)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})

    await anon_client.post("/api/openrouter/reveal")

    journal = await _journal(anon_client)
    assert "показ" in journal.lower() or "показан" in journal.lower(), (
        "показ ключа не попал в журнал — по журналу нельзя восстановить, "
        f"когда секрет покидал сервер: {journal[:300]}"
    )
    assert OPENROUTER_KEY not in journal, "сам ключ утёк в журнал"


async def test_revealed_secret_belongs_to_the_asking_user(
    anon_client, db, user, user_b, allow_webhook
):
    await act_as(anon_client, db, user)
    await anon_client.post("/api/openrouter", json={"api_key": OPENROUTER_KEY})

    await act_as(anon_client, db, user_b)
    shown = await anon_client.post("/api/openrouter/reveal")
    assert shown.json()["secret"] == "", "показан чужой ключ"
