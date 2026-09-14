"""Уйти и забрать своё (задача 13.2).

Открытый вопрос №4 плана, закрытый наполовину: автоочистка по сроку была,
экспорта и удаления аккаунта — нет. `app/api/auth.py` знал только `/auth/me`
и `/auth/logout`.

Для **этого** сервиса удаление — не вежливость, а способ **отозвать доступ**.
На сервере лежит ключ от живого Telegram-аккаунта, и пока строку не удалили,
сервис может им пользоваться. Поэтому здесь два свойства, которых нет у
обычного «удалите мой профиль»:

1. **Удаление завершает сессию в самом Telegram.** Стереть строку — значит
   убрать доступ у СЕРВИСА, но сессия остаётся в списке устройств, а любая
   уцелевшая копия строки (резервная копия базы!) продолжает работать.
   `log_out` закрывает и это.
2. **Недоступный Telegram не держит человека в заложниках.** Если завершить
   сессию не вышло, удаление всё равно идёт до конца: право уйти не зависит
   от того, отвечает ли чужой сервис. Но молчать об этом нельзя — остаётся
   запись в логе процесса (журнал тенанта к тому моменту уже удаляется).

И общее для обоих эндпоинтов: **экспорт не выдаёт секретов**. Он задуман как
«забрать своё», а токен бота и ключ OpenRouter — не данные, а доступы.
"""

import pytest
from sqlalchemy import func, select
from test_112_map_reduce import _source

from app.models import FeedItem, Integration, LogEntry, Monitor, TelegramAccount, User
from app.services.integrations import save_integration_secrets

pytestmark = pytest.mark.asyncio

BOT_TOKEN = "123456:AAsecret-bot-token"
OPENROUTER_KEY = "sk-or-v1-secret-key"


class RecordingRevoker:
    """Двойник завершения сессии в Telegram."""

    def __init__(self, fail: bool = False):
        self.calls: list[int] = []
        self.fail = fail

    async def __call__(self, db, user_id):
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.calls.append(user_id)
        return True


async def _fill(db, user) -> None:
    """Немного данных каждого вида — иначе проверять нечего."""
    await save_integration_secrets(
        db, user.id, bot_token=BOT_TOKEN, openrouter_api_key=OPENROUTER_KEY
    )
    await _source(
        db, user, public_id=f"src-{user.id}", channels=[("@alpha", -1001, 5, "искать")]
    )
    db.add(
        FeedItem(
            user_id=user.id,
            job_id=f"job-{user.id}",
            chat_id=-1001,
            chat_title="Канал",
            messages_count=1,
            ai_analysis="находка",
            raw_messages_json="[]",
        )
    )
    db.add(
        TelegramAccount(
            user_id=user.id,
            phone="+70000000000",
            session_string_encrypted="зашифровано",
            tg_user_id=1,
        )
    )
    db.add(LogEntry(user_id=user.id, event_type="X", status="INFO", details="событие"))
    await db.commit()


# --------------------------------------------------------------------------
# Экспорт
# --------------------------------------------------------------------------


async def test_export_returns_what_the_person_made(anon_client, db, user):
    from conftest import act_as

    await act_as(anon_client, db, user)
    await _fill(db, user)

    response = await anon_client.get("/api/account/export")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == user.email
    assert body["sources"] and body["sources"][0]["public_id"] == f"src-{user.id}"
    assert body["sources"][0]["channels"], "источник приехал без каналов"
    assert body["feed"] and body["feed"][0]["ai_analysis"] == "находка"
    assert body["journal"], "журнал не выгружен"


async def test_export_never_hands_out_access(anon_client, db, user):
    """Токен бота и ключ модели — доступы, а не данные человека."""
    from conftest import act_as

    await act_as(anon_client, db, user)
    await _fill(db, user)

    raw = (await anon_client.get("/api/account/export")).text
    for secret in (BOT_TOKEN, OPENROUTER_KEY, "зашифровано"):
        assert secret not in raw, f"экспорт выдал доступ: {secret[:12]}…"


async def test_export_shows_only_your_own(anon_client, db, user, user_b):
    from conftest import act_as

    await _fill(db, user_b)
    await act_as(anon_client, db, user)
    await _fill(db, user)

    body = (await anon_client.get("/api/account/export")).json()
    assert [s["public_id"] for s in body["sources"]] == [f"src-{user.id}"], (
        f"в выгрузку попал чужой источник: {body['sources']}"
    )


async def test_export_is_closed_to_anonymous(anon_client):
    assert (await anon_client.get("/api/account/export")).status_code == 401


# --------------------------------------------------------------------------
# Удаление
# --------------------------------------------------------------------------


async def test_deletion_needs_a_deliberate_confirmation(anon_client, db, user):
    """Необратимое действие не должно случаться с одного нажатия."""
    from conftest import act_as

    await act_as(anon_client, db, user)
    await _fill(db, user)

    response = await anon_client.request(
        "DELETE", "/api/account", json={"confirm": "нет"}
    )
    assert response.status_code == 400, response.text
    assert await db.scalar(select(func.count()).select_from(Monitor)) == 1, (
        "данные удалены при неверном подтверждении"
    )


async def test_deletion_takes_everything_of_that_tenant(anon_client, db, user, user_b):
    from conftest import act_as

    await _fill(db, user_b)
    await act_as(anon_client, db, user)
    await _fill(db, user)

    response = await anon_client.request(
        "DELETE", "/api/account", json={"confirm": user.email}
    )
    assert response.status_code == 200, response.text

    for model in (Monitor, FeedItem, LogEntry, Integration, TelegramAccount):
        mine = await db.scalar(
            select(func.count()).select_from(model).where(model.user_id == user.id)
        )
        assert mine == 0, f"{model.__name__}: строки тенанта остались после удаления"
        theirs = await db.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_b.id)
        )
        assert theirs >= 1, f"{model.__name__}: удаление задело чужого тенанта"

    assert (
        await db.scalar(
            select(func.count()).select_from(User).where(User.id == user.id)
        )
        == 0
    )
    assert (
        await db.scalar(
            select(func.count()).select_from(User).where(User.id == user_b.id)
        )
        == 1
    )


async def test_deletion_closes_the_telegram_session(db, user):
    """Стереть строку — убрать доступ у сервиса. Сессия при этом жива.

    Она остаётся в списке устройств, а уцелевшая копия строки (резервная
    копия базы) продолжает работать. `log_out` закрывает и это.
    """
    from app.services.account import delete_account

    await _fill(db, user)
    revoker = RecordingRevoker()
    await delete_account(db, user, revoker=revoker)
    assert revoker.calls == [user.id], "сессия в Telegram не завершена"


async def test_an_unreachable_telegram_does_not_trap_the_person(db, user):
    """Право уйти не зависит от того, отвечает ли чужой сервис."""
    from app.services.account import delete_account

    await _fill(db, user)
    await delete_account(db, user, revoker=RecordingRevoker(fail=True))
    assert await db.scalar(select(func.count()).select_from(Monitor)) == 0, (
        "недоступный Telegram заблокировал удаление аккаунта"
    )


async def test_deletion_is_closed_to_anonymous(anon_client):
    response = await anon_client.request(
        "DELETE", "/api/account", json={"confirm": "x"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Интерфейс: до эндпоинта должно быть куда нажать
# --------------------------------------------------------------------------


async def test_the_cabinet_offers_both_actions():
    """Право, до которого не дойти из кабинета, правом не является."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    index = (root / "static" / "index.html").read_text(encoding="utf-8")
    main_js = (root / "static" / "js" / "main.js").read_text(encoding="utf-8")
    module = (root / "static" / "js" / "account.js").read_text(encoding="utf-8")

    assert 'id="exportAccountBtn"' in index, "нет кнопки выгрузки"
    assert 'id="deleteAccountBtn"' in index, "нет кнопки удаления"
    assert "account.js" in main_js, "модуль аккаунта никуда не подключён"
    assert "/api/account/export" in module and "/api/account" in module

    block_start = index.index('id="exportAccountBtn"') - 1200
    block = index[max(0, block_start) : index.index('id="deleteAccountBtn"') + 200]
    assert not __import__("re").search(r"\son(click|change)=", block), (
        "инлайновый обработчик — под CSP он мёртв"
    )


async def test_deleting_from_the_cabinet_asks_for_the_address():
    """Кнопка «удалить» рядом с кнопкой «выгрузить» — не место для оплошности."""
    import pathlib

    module = (
        pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "account.js"
    ).read_text(encoding="utf-8")
    assert "prompt(" in module, "удаление не спрашивает подтверждения у человека"
    assert "confirm:" in module or '"confirm"' in module, (
        "подтверждение не уходит на сервер — он откажет, и кнопка просто не сработает"
    )
