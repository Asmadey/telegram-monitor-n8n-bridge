"""Задача 3.2 — MTProto-сессия в БД, зашифрованная.

Замена файла personal_account.session: на сервере оседают сессии ЧУЖИХ
Telegram-аккаунтов, их нельзя сбросить удалённо — утечка открытого текста
равна компрометации всех аккаунтов разом. Поэтому строка сессии хранится
только зашифрованной, ключ — в ENV, в БД его нет никогда.

Третий тест — свип утечек: строка сессии не должна появиться ни в одном
ответе API (сейчас /api/telegram/status и /api/settings не существуют —
404 не течёт; тест активируется автоматически, когда их перенесут).
"""

import pytest
from sqlalchemy import text

from app.security.sessions import SESSION_COOKIE, create_session, sign_session_id

SESSION_STRING = "1BQANOTEuMTA4LjU2LjE4NAG7-test-session-string-XYZ"


@pytest.mark.asyncio
async def test_session_string_is_encrypted_at_rest(db, user):
    from app.security.crypto import decrypt
    from app.services.tg_account import save_tg_session

    await save_tg_session(db, user.id, SESSION_STRING)
    raw = (
        await db.execute(
            text(
                "SELECT session_string_encrypted "
                "FROM telegram_accounts WHERE user_id = :u"
            ),
            {"u": user.id},
        )
    ).scalar()
    assert raw, "строка telegram_accounts не создана"
    assert SESSION_STRING not in raw, (
        "MTProto-сессия лежит открытым текстом — утечка базы "
        "компрометирует все аккаунты тенантов разом"
    )
    assert decrypt(raw) == SESSION_STRING, "дешифровка не вернула исходную строку"


@pytest.mark.asyncio
async def test_save_tg_session_is_upsert_per_user(db, user):
    """У юзера один Telegram-аккаунт (unique user_id): повторное сохранение —
    UPDATE той же строки, а не вторая строка и не IntegrityError."""
    from app.services.tg_account import save_tg_session

    await save_tg_session(db, user.id, SESSION_STRING)
    await save_tg_session(db, user.id, "another-session-string-2")
    count = (
        await db.execute(
            text("SELECT COUNT(*) FROM telegram_accounts WHERE user_id = :u"),
            {"u": user.id},
        )
    ).scalar()
    assert count == 1, (
        f"у юзера {count} строк telegram_accounts — должен быть 1 (upsert)"
    )


@pytest.mark.asyncio
async def test_session_string_never_appears_in_any_api_response(anon_client, db, user):
    from app.services.tg_account import save_tg_session

    await save_tg_session(db, user.id, SESSION_STRING)
    session = await create_session(db, user, ip="127.0.0.1", user_agent="pytest")
    anon_client.cookies.set(SESSION_COOKIE, sign_session_id(session.id))
    for path in ("/api/telegram/status", "/api/settings", "/auth/me", "/health"):
        r = await anon_client.get(path)
        assert SESSION_STRING not in r.text, (
            f"строка MTProto-сессии утекла в ответе {path} ({r.status_code})"
        )
