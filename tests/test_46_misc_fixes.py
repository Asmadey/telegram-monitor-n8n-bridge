"""Задача 4.7 — прочие исправления корректности (PLAN.md).

Четыре дефекта оригинала, перенесённых в новую сборку:

1. С16: `get_setting` оригинала (server.py:267) считал `""` за «не
   задано» и уходил в fallback — очистить webhook_url было НЕЛЬЗЯ.
   В новой сборке роль «записи секрета» играет save_integration_secrets,
   и она наследует дефект: `if webhook_url:` глотает "" так же, как
   None. Фикс: None — «не передан» (не трогаем), "" — «очистить».
2. `update_integrations_config` оригинала (server.py:251) строил
   `SET {k} = ?` из ключей словаря — имя колонки из пользовательских
   данных. Порт обязан идти через белый список: неизвестный ключ —
   ошибка; секретные колонки — только через save_integration_secrets.
3. С22: `/health` оригинала (server.py:1075) отдавал анониму id,
   first_name, username аккаунта. Новая сборка уже отвечает
   {"status": "ok"} (задача 2.3) — трипваер держит это навсегда.
4. С23: `iter_messages` + `break` по времени (server.py:706) обрывается
   на закреплённом сообщении: пин старее cutoff приходит РАНЬШЕ свежих
   постов — break теряет ВСЁ новое. Фикс: `continue` + ограничение
   limit (iter_messages и так вернёт не больше limit сообщений).
"""

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Integration
from app.services.integrations import (
    integration_secrets,
    save_integration_secrets,
    update_integration_config,
)
from app.services.messages import fetch_channel_messages

NOW = datetime.datetime.now(datetime.timezone.utc)


async def _integration(db, user_id: int) -> Integration:
    return (
        await db.scalars(select(Integration).where(Integration.user_id == user_id))
    ).first()


# --- 1. С16: "" очищает секрет, None — не трогает -----------------------------


# kwarg записи → ключ в расшифрованном dict (telegram_bot_token, не bot_token)
_SECRETS_KEY = {
    "bot_token": "telegram_bot_token",
    "openrouter_api_key": "openrouter_api_key",
    "webhook_url": "webhook_url",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["bot_token", "openrouter_api_key", "webhook_url"],
)
async def test_empty_string_clears_secret(db, user_a, field):
    """ПЛАН: нельзя очистить webhook_url — "" обязан означать «очистить»,
    иначе пользователь навсегда привязан к первому URL."""
    await save_integration_secrets(db, user_a.id, **{field: "живое-значение"})
    await save_integration_secrets(db, user_a.id, **{field: ""})
    row = await _integration(db, user_a.id)
    assert integration_secrets(row)[_SECRETS_KEY[field]] == "", (
        f"пустая строка не очистила {field}: секрет невозможно убрать"
    )


@pytest.mark.asyncio
async def test_none_keeps_stored_secret(db, user_a):
    """Обратная сторона контракта: None — «поле не передано» — не должен
    затирать записанный секрет (пустой POST не вытирает ключ, 0.3)."""
    await save_integration_secrets(
        db, user_a.id, webhook_url="https://n8n.example/hook", bot_token="111:AAAA"
    )
    await save_integration_secrets(db, user_a.id, bot_token="222:BBBB")
    secrets = integration_secrets(await _integration(db, user_a.id))
    assert secrets["webhook_url"] == "https://n8n.example/hook"
    assert secrets["telegram_bot_token"] == "222:BBBB"


# --- 2. Белый список колонок update_integration_config ------------------------


@pytest.mark.asyncio
async def test_update_config_applies_whitelisted_columns(db, user_a):
    """Несекретные настройки применяются по имени — но только из белого
    списка (telegram_sender_id, openrouter_base_url/model/enabled,
    telegram_forward_enabled, auto_webhook_enabled)."""
    row = await update_integration_config(
        db,
        user_a.id,
        {
            "openrouter_enabled": True,
            "openrouter_model": "vendor/model-x",
            "telegram_sender_id": "12345",
        },
    )
    assert row.openrouter_enabled is True
    assert row.openrouter_model == "vendor/model-x"
    assert row.telegram_sender_id == "12345"


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_keys(db, user_a):
    """Ключ словаря — пользовательские данные. `SET {k} = ?` оригинала
    позволял писать ЛЮБУЮ колонку; порт обязан ломаться громко, а не
    молча подменять user_id (это переносило бы строку в чужой тенант)."""
    await save_integration_secrets(db, user_a.id, webhook_url="https://n8n/hook")
    with pytest.raises(ValueError):
        await update_integration_config(db, user_a.id, {"user_id": 999999})
    row = await _integration(db, user_a.id)
    assert row.user_id == user_a.id, "user_id подменён через ключи словаря"


@pytest.mark.asyncio
async def test_update_config_rejects_secret_columns(db, user_a):
    """Секретные поля — только через save_integration_secrets (единственная
    точка шифрования, 3.4): конфигурационный путь не должен уметь записать
    ни зашифрованные, ни «придуманные» открытые колонки для них."""
    await save_integration_secrets(db, user_a.id, webhook_url="https://n8n/hook")
    with pytest.raises(ValueError):
        await update_integration_config(
            db,
            user_a.id,
            {"webhook_url": "https://evil.example/hook"},
        )
    with pytest.raises(ValueError):
        await update_integration_config(
            db,
            user_a.id,
            {"openrouter_api_key": "sk-or-attacker"},
        )
    secrets = integration_secrets(await _integration(db, user_a.id))
    assert secrets["webhook_url"] == "https://n8n/hook"
    assert secrets["openrouter_api_key"] == ""


# --- 3. С23: закреплённое сообщение не обрывает выборку ----------------------


class _Msg:
    """Минимальный двойник telethon-сообщения для офлайн-тестов."""

    def __init__(self, mid, date, text, *, sender_name="Автор", out=False):
        self.id = mid
        self.date = date
        self.text = text
        self.out = out
        self.sender_id = 42
        self.media = None
        self.views = 10
        self.forwards = 1
        self.reactions = None
        self._sender = SimpleNamespace(first_name=sender_name, title=None)

    async def get_sender(self):
        return self._sender


class _FakeClient:
    """Клиент с iter_messages: выдаёт подготовленную ленту и запоминает
    limit — выборка приходит из Telethon, в тестах подменяется."""

    def __init__(self, messages):
        self._messages = messages
        self.seen_limit = None

    def iter_messages(self, entity, limit=None):
        self.seen_limit = limit

        async def gen():
            for m in self._messages:
                yield m

        return gen()


ENTITY = SimpleNamespace(username="channel", id=-100123, title="Канал")


@pytest.mark.asyncio
async def test_old_pinned_message_does_not_stop_fetch():
    """СЦЕНАРИЙ ДЕФЕКТА: закреп старее cutoff приходит РАНЬШЕ свежих
    постов — `break` оригинала теряет ВСЁ новое. Свежие обязаны дойти."""
    pinned = _Msg(1, NOW - datetime.timedelta(hours=48), "закреп")
    fresh = [
        _Msg(2, NOW - datetime.timedelta(hours=1), "новый пост"),
        _Msg(3, NOW - datetime.timedelta(hours=2), "ещё пост"),
    ]
    client = _FakeClient([pinned, *fresh])
    got = await fetch_channel_messages(
        client, ENTITY, limit=20, offset_hours=24, now=NOW
    )
    assert [m["id"] for m in got] == [2, 3], (
        "закреплённое сообщение оборвало выборку — свежие посты потеряны"
    )


@pytest.mark.asyncio
async def test_all_old_messages_return_empty():
    """Лента целиком за cutoff — пустой результат, ничего не падает."""
    client = _FakeClient(
        [
            _Msg(1, NOW - datetime.timedelta(hours=25), "старьё"),
            _Msg(2, NOW - datetime.timedelta(hours=30), "ещё старьё"),
        ]
    )
    got = await fetch_channel_messages(
        client, ENTITY, limit=20, offset_hours=24, now=NOW
    )
    assert got == []


@pytest.mark.asyncio
async def test_limit_is_passed_to_client():
    """Ограничение по limit: выборка не читает канал бесконечно — limit
    уходит в iter_messages (порт оригинала)."""
    client = _FakeClient([_Msg(1, NOW, "пост")])
    await fetch_channel_messages(client, ENTITY, limit=20, now=NOW)
    assert client.seen_limit == 20


@pytest.mark.asyncio
async def test_message_fields_mapped():
    """Порт маппинга оригинала: id, дата ISO, текст, отправитель,
    ссылка на пост — данные, на которые опирается диспетчеризация."""
    client = _FakeClient([_Msg(7, NOW - datetime.timedelta(minutes=5), "текст поста")])
    got = await fetch_channel_messages(client, ENTITY, limit=20, now=NOW)
    assert len(got) == 1
    m = got[0]
    assert m["id"] == 7
    assert m["text"] == "текст поста"
    assert m["sender"] == "Автор"
    assert m["post_url"] == "https://t.me/channel/7"
    assert m["date"].startswith(str(NOW.year))


# --- 4. С22: /health — трипваер ------------------------------------------------


@pytest.mark.asyncio
async def test_health_is_status_only(raw_client):
    """ТРИПВАЕР (новая сборка уже отвечает правильно — задача 2.3):
    аноним получает ТОЛЬКО статус. id/first_name/username живого
    аккаунта из /health — разведданные для любого, кто открыл страницу."""
    resp = await raw_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}, (
        f"/health раскрывает больше, чем статус: {resp.json()}"
    )
