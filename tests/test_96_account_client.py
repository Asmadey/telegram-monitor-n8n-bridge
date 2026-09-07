"""Действия аккаунта идут сессией аккаунта, а не сессией входа (9.9).

Найдено владельцем сразу после первого успешного входа: добавление канала
падало с `AUTH_KEY_UNREGISTERED` — «The key is not registered in the system
(caused by ResolveUsernameRequest)».

Причина. Разрешение канала брало клиента у зависимости ВХОДА
(`get_telegram_auth_client`). Она поднимает клиента текущей попытки входа,
а успешный вход эту попытку съедает — значит после подключения аккаунта
зависимость отдавала клиента с ПУСТОЙ сессией. Telegram отвечал ровно то,
что и должен: у этого ключа нет зарегистрированного пользователя.

В монолите этого класса ошибок не было: там жил один глобальный
авторизованный клиент, и вопрос «чьей сессией выполняется действие» не
возникал. При разделении на два процесса и короткоживущие клиенты он стал
главным — и различать надо ровно два случая:

- клиент ВХОДА — пустая сессия либо сессия незавершённой попытки;
- клиент АККАУНТА — сохранённая сессия подключённого пользователя.

Тесты этого не ловили, потому что подменяли сам резолвер: под двойником
вопрос об источнике сессии не возникает вовсе.
"""

import types

import pytest
from conftest import act_as

from app.models import TelegramAccount

pytestmark = pytest.mark.asyncio

ACCOUNT_SESSION = "1BQANOTEuMTA4LjU2LjE4NAG7-authorized-account-session"
API_HASH = "c" * 32


class RecordingClient:
    def __init__(self, session_string: str = ""):
        self.built_from = session_string
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def get_entity(self, target):
        return types.SimpleNamespace(id=-100123, title="Канал", username="theyseeku")


async def _connected_account(db, user):
    from app.security.crypto import encrypt
    from app.services.tg_credentials import save_credentials

    await save_credentials(db, user.id, api_id=1234567, api_hash=API_HASH)
    db.add(
        TelegramAccount(
            user_id=user.id,
            phone="+79990000000",
            session_string_encrypted=encrypt(ACCOUNT_SESSION),
            tg_user_id=777,
            tg_username="owner",
            status="active",
        )
    )
    await db.commit()


def _patch_telethon(monkeypatch, module, built):
    def _factory(session, api_id, api_hash):
        client = RecordingClient(getattr(session, "save", lambda: "")())
        built.append(client)
        return client

    monkeypatch.setattr(module, "TelegramClient", _factory)
    monkeypatch.setattr(
        module,
        "StringSession",
        lambda value="": types.SimpleNamespace(save=lambda: value),
    )


async def test_account_client_uses_the_connected_session(db, user, monkeypatch):
    """Клиент аккаунта строится из сохранённой сессии пользователя."""
    from app.services import tg_auth

    await _connected_account(db, user)
    built: list[RecordingClient] = []
    _patch_telethon(monkeypatch, tg_auth, built)

    generator = tg_auth.get_account_client(user=user, db=db)
    client = await generator.__anext__()
    try:
        assert built[0].built_from == ACCOUNT_SESSION, (
            "действие выполняется чужой (пустой) сессией — Telegram ответит "
            "AUTH_KEY_UNREGISTERED"
        )
        assert client.connected, "клиент не подключён"
    finally:
        with pytest.raises(StopAsyncIteration):
            await generator.__anext__()
    assert built[0].disconnected, (
        "короткоживущий клиент не отключён: долгоживущие принадлежат воркеру"
    )


async def test_without_a_connected_account_the_answer_is_explicit(
    db, user, monkeypatch
):
    """Отказ понятный, а не загадочная ошибка Telegram."""
    from fastapi import HTTPException

    from app.services import tg_auth
    from app.services.tg_credentials import save_credentials

    await save_credentials(db, user.id, api_id=1234567, api_hash=API_HASH)
    await db.commit()
    _patch_telethon(monkeypatch, tg_auth, [])

    generator = tg_auth.get_account_client(user=user, db=db)
    with pytest.raises(HTTPException) as failure:
        await generator.__anext__()
    assert failure.value.status_code == 400
    assert "не подключ" in failure.value.detail.lower(), failure.value.detail


async def test_resolving_a_channel_no_longer_borrows_the_login_client(
    anon_client, db, user, monkeypatch
):
    """Сквозная проверка: добавление канала идёт сессией аккаунта.

    Двойник подменяет не резолвер, а сам Telethon — иначе вопрос о том,
    чьей сессией выполняется действие, снова окажется вне теста (ровно так
    дефект и дожил до прода).
    """
    from app.services import tg_auth

    await _connected_account(db, user)
    await act_as(anon_client, db, user)

    built: list[RecordingClient] = []
    _patch_telethon(monkeypatch, tg_auth, built)

    created = await anon_client.post(
        "/api/monitors",
        json={"chat_target": "https://t.me/theyseeku", "interval_minutes": 60},
    )

    assert created.status_code in (200, 201), created.text
    assert built, "клиент вообще не создавался"
    assert built[0].built_from == ACCOUNT_SESSION, (
        "канал разрешался клиентом входа: после успешного входа его сессия "
        "пуста, отсюда AUTH_KEY_UNREGISTERED"
    )


def test_login_client_is_used_only_by_the_login_routes():
    """Свип: клиент входа не должен появиться нигде, кроме входа.

    Ловит не сегодняшний резолвер, а завтрашний эндпоинт, написанный по
    образцу соседнего кода: обе зависимости называются похоже, отличаются
    одним словом, а разница между ними — рабочий эндпоинт против
    AUTH_KEY_UNREGISTERED.
    """
    import pathlib

    api = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
    offenders = []
    for module in sorted(api.glob("*.py")):
        if module.name == "telegram.py":  # только там живут send-code и sign-in
            continue
        for number, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), 1
        ):
            if "get_telegram_auth_client" in line:
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert not offenders, (
        "действие аккаунта берёт клиента входа — его сессия пуста после "
        "подключения:\n" + "\n".join(offenders)
    )
