"""Код подтверждения принадлежит той MTProto-сессии, которая его запросила.

Найдено владельцем на живом Telegram 2026-09-07: код приходил, вводился
вовремя — и `sign_in` отвечал `The confirmation code has expired
(PHONE_CODE_EXPIRED)`.

Причина не в сроке. Telegram привязывает попытку входа к auth-key той
сессии, которая вызвала `send_code_request`. Веб-процесс поднимает
короткоживущий клиент на КАЖДЫЙ запрос (так и задумано, 4.1: долгоживущие
клиенты принадлежат воркеру) — значит `send-code` и `sign-in` работали с
двумя РАЗНЫМИ пустыми `StringSession`, с разными auth-key. Для второй
сессии код не существовал никогда, и Telegram честно отвечал «истёк».

Почему этого не видели тесты: двойник подменял зависимость целиком и
возвращал ОДИН объект на оба шага. Он воспроизводил не устройство
рантайма, а удобное предположение — и потому был зелёным ровно там, где
живой Telegram отказывал. Отсюда контракт ниже: проверяется не «шаги
прошли», а «второй шаг работает той же сессией, что и первый».

Сама строка сессии — секрет (по ней можно завершить чужой вход), поэтому
в базе лежит зашифрованной, как и сессия подключённого аккаунта (3.4).
"""

import types

import pytest
from conftest import act_as
from sqlalchemy import select

from app.models import TgAuthAttempt
from app.security.crypto import decrypt

pytestmark = pytest.mark.asyncio

SENT_SESSION = "1BQANOTEuMTA4LjU2LjE4NAG7-fake-session-of-the-attempt"
API_HASH = "b" * 32


class RecordingClient:
    """Двойник, помнящий, ИЗ КАКОЙ строки сессии он построен."""

    def __init__(self, session_string: str = ""):
        self.built_from = session_string
        self.session = types.SimpleNamespace(save=lambda: SENT_SESSION)
        self.sign_in_calls: list[dict] = []
        self.authorized = False

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def send_code_request(self, phone: str):
        return types.SimpleNamespace(phone_code_hash=f"pch-{phone}")

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        self.authorized = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        return types.SimpleNamespace(id=777, first_name="Tg", username="tester")


async def _with_credentials(db, user):
    from app.services.tg_credentials import save_credentials

    await save_credentials(db, user.id, api_id=1234567, api_hash=API_HASH)
    await db.commit()


async def test_send_code_keeps_the_session_that_asked_for_the_code(
    anon_client, db, user, monkeypatch
):
    """Без сохранённой сессии второй шаг начинать нечем."""
    from app.main import app
    from app.services.tg_auth import get_telegram_auth_client

    await _with_credentials(db, user)
    await act_as(anon_client, db, user)

    client = RecordingClient()

    async def _override():
        yield client

    app.dependency_overrides[get_telegram_auth_client] = _override
    try:
        sent = await anon_client.post(
            "/api/telegram/send-code", json={"phone": "+79990000000"}
        )
    finally:
        app.dependency_overrides.pop(get_telegram_auth_client, None)

    assert sent.status_code == 200, sent.text
    attempt = (await db.scalars(select(TgAuthAttempt))).first()
    assert attempt is not None, "попытка входа не сохранена"
    stored = attempt.session_string_encrypted
    assert stored, (
        "сессия, запросившая код, не сохранена — подтверждать код будет "
        "другая сессия, и Telegram ответит PHONE_CODE_EXPIRED"
    )
    assert SENT_SESSION not in stored, "строка сессии лежит открытым текстом"
    assert decrypt(stored) == SENT_SESSION


async def test_login_client_is_restored_from_the_attempt(db, user, monkeypatch):
    """Клиент второго шага строится из сессии первого, а не с нуля."""
    import datetime

    from app.security.crypto import encrypt
    from app.services import tg_auth

    await _with_credentials(db, user)
    db.add(
        TgAuthAttempt(
            user_id=user.id,
            phone="+79990000000",
            phone_code_hash="pch",
            session_string_encrypted=encrypt(SENT_SESSION),
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=10),
        )
    )
    await db.commit()

    built: list[RecordingClient] = []

    def _factory(session, api_id, api_hash):
        # Telethon принимает объект StringSession; двойник запоминает
        # строку, из которой тот построен.
        client = RecordingClient(getattr(session, "save", lambda: "")())
        built.append(client)
        return client

    monkeypatch.setattr(tg_auth, "TelegramClient", _factory)
    monkeypatch.setattr(
        tg_auth,
        "StringSession",
        lambda value="": types.SimpleNamespace(save=lambda: value),
    )

    generator = tg_auth.get_telegram_auth_client(user=user, db=db)
    await generator.__anext__()
    try:
        assert built, "клиент не создан"
        assert built[0].built_from == SENT_SESSION, (
            "вход продолжает ДРУГАЯ сессия: для неё код подтверждения не "
            "существовал никогда — это и есть PHONE_CODE_EXPIRED"
        )
    finally:
        with pytest.raises(StopAsyncIteration):
            await generator.__anext__()


async def test_fresh_attempt_starts_from_an_empty_session(db, user, monkeypatch):
    """Нет попытки — нет и сессии: первый шаг начинается с чистой."""
    import types as _types

    from app.services import tg_auth

    await _with_credentials(db, user)

    built: list[str] = []

    def _factory(session, api_id, api_hash):
        built.append(getattr(session, "save", lambda: "")())
        return RecordingClient()

    monkeypatch.setattr(tg_auth, "TelegramClient", _factory)
    monkeypatch.setattr(
        tg_auth,
        "StringSession",
        lambda value="": _types.SimpleNamespace(save=lambda: value),
    )

    generator = tg_auth.get_telegram_auth_client(user=user, db=db)
    await generator.__anext__()
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert built == [""], f"первый шаг начат не с чистой сессии: {built}"
