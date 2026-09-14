"""Канал, добавленный числовым id, обязан разрешаться (найдено владельцем).

Источник «VASILE LUNGO | INSIDER» не дал ни одной находки, а в канале были
свежие публикации. В тревоге канал назван голым числом — `-1004324020362`, —
и это само по себе диагноз: имя берётся как `chat_title or chat_target`, а
`chat_title` появляется только после **успешного** разрешения. Значит канал не
разрешался ни разу, а `chat_target` — то, что человек ввёл: числовой id.

Две причины, и по отдельности ни одна не лечится:

1. **Id хранится строкой и строкой уезжает в Telethon.** `get_entity("-100…")`
   разбирает строку как имя пользователя или телефон — числовой id туда не
   попадает вовсе. Функция `clean_target`, которая ровно для этого и написана,
   **не вызывается ниоткуда**: она мёртвая с переезда 11.8.
2. **По голому id канал не разрешить без `access_hash`.** Telethon пробует
   `GetChannelsRequest` с `access_hash=0` и для приватного канала получает
   `ChannelInvalidError`. Кэш сущностей живёт в сессии, а `StringSession`
   его **не сохраняет** — после каждого перезапуска он пуст. Лечится
   прогревом: список диалогов наполняет кэш тем, куда аккаунт и так входит.

И отдельная беда рядом: `clean_target` калечит пригласительные ссылки
приватных каналов — `t.me/+AbCdEf` превращается в `AbCdEf`. Пока функция
мертва, это никому не мешало; включать её в таком виде нельзя.
"""

import pytest

from app.services.channels import clean_target

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Разбор адреса
# --------------------------------------------------------------------------


def test_a_numeric_target_becomes_a_number():
    assert clean_target("-1004324020362") == -1004324020362
    assert clean_target(" -1004324020362 ") == -1004324020362


def test_a_username_stays_a_username():
    assert clean_target("@alpha") == "alpha"
    assert clean_target("https://t.me/alpha") == "alpha"
    assert clean_target("t.me/alpha/") == "alpha"


def test_an_invite_link_keeps_its_plus():
    """`+` — часть хеша приглашения, а не мусор.

    Без него приватный канал, добавленный единственным доступным способом,
    не разрешится никогда: `AbCdEf` — не имя пользователя.
    """
    assert clean_target("https://t.me/+AbCdEf") == "+AbCdEf"
    assert clean_target("t.me/joinchat/AbCdEf") == "joinchat/AbCdEf"


async def test_the_api_keeps_the_address_as_typed(anon_client, db, user):
    """Ошибка первой версии теста, исправлена в тесте.

    Она требовала, чтобы API сохранял *приведённый* адрес, — и ломала
    существующий контракт: `test_114` проверяет, что в списке человек видит
    то, что набрал (`@alpha`, а не `alpha`). Приведение ничего не давало бы и
    для дела: Telethon одинаково понимает `@alpha`, `alpha` и полную ссылку.

    Лечится не хранение, а РАЗРЕШЕНИЕ: числовой id нужно пробовать числом, и
    это работает для строк, которые уже лежат в базе.
    """
    from conftest import act_as

    await act_as(anon_client, db, user)
    created = await anon_client.post(
        "/api/sources", json={"title": "Источник", "interval_minutes": 60}
    )
    source_id = created.json()["public_id"]
    added = await anon_client.post(
        f"/api/sources/{source_id}/channels",
        json={"chat_target": "https://t.me/alpha"},
    )
    assert added.status_code in (200, 201), added.text
    stored = added.json()
    chat_target = (stored.get("channel") or stored).get("chat_target")
    assert chat_target == "https://t.me/alpha", (
        f"адрес переписан молча — человек не узнает в списке то, что набрал: {stored}"
    )


# --------------------------------------------------------------------------
# Разрешение в конвейере
# --------------------------------------------------------------------------


class CountingGateway:
    """Двойник шлюза: помнит, чем его просили разрешить канал."""

    def __init__(self, works_with=None, dialogs_needed: bool = False):
        self.tried: list = []
        self.works_with = works_with
        self.dialogs_needed = dialogs_needed
        self.primed = 0

    async def client_for(self, db, user_id):
        return object()

    async def prime(self, client):
        self.primed += 1

    async def resolve(self, client, target):
        self.tried.append(target)
        if self.dialogs_needed and not self.primed:
            raise ValueError("Could not find the input entity for PeerChannel")
        if self.works_with is not None and target != self.works_with:
            raise ValueError(f"Cannot find any entity corresponding to {target!r}")

        class _Entity:
            id = -1004324020362
            title = "VASILE LUNGO | INSIDER"
            username = None

        return _Entity()

    async def fetch(self, client, entity, *, limit, offset_hours):
        return []

    async def avatar(self, client, entity):
        return None


async def test_a_numeric_string_is_also_tried_as_a_number(db, user):
    """Строка «-100…» для Telethon — не id, а испорченное имя.

    Старые строки уже лежат в базе, и миграции им не будет: разрешение
    обязано само догадаться, что перед ним число.
    """
    from test_58_worker_body import _worker

    from app.worker import Worker

    gateway = CountingGateway(works_with=-1004324020362)
    worker: Worker = _worker(db, telegram=gateway)
    entity = await worker._resolve_channel(
        object(), {"chat_id": None, "chat_target": "-1004324020362"}
    )
    assert entity.id == -1004324020362
    assert -1004324020362 in gateway.tried, (
        f"числовой адрес так и не попробовали числом: {gateway.tried}"
    )


async def test_the_entity_cache_is_primed_before_giving_up(db, user):
    """`StringSession` не хранит кэш сущностей — после перезапуска он пуст.

    Список диалогов наполняет его тем, куда аккаунт и так входит; без этого
    приватный канал по id не разрешить никогда.
    """
    from test_58_worker_body import _worker

    gateway = CountingGateway(dialogs_needed=True)
    worker = _worker(db, telegram=gateway)
    entity = await worker._resolve_channel(
        object(), {"chat_id": -1004324020362, "chat_target": "-1004324020362"}
    )
    assert gateway.primed == 1, "кэш сущностей не прогревали — отказ принят как есть"
    assert entity.title == "VASILE LUNGO | INSIDER"


async def test_a_working_channel_does_not_pay_for_priming(db, user):
    """Антивакуум: прогрев на каждый канал — лишний запрос к Telegram."""
    from test_58_worker_body import _worker

    gateway = CountingGateway()
    worker = _worker(db, telegram=gateway)
    await worker._resolve_channel(object(), {"chat_id": None, "chat_target": "alpha"})
    assert gateway.primed == 0, "прогрев случился там, где и так всё разрешилось"


async def test_the_alert_names_the_reason_not_the_journal(db, user):
    """«Причина в журнале» заставляет идти за ней второй раз.

    Тревога существует затем, чтобы человек узнал об отказе, не заходя в
    кабинет. Отправить его в кабинет за причиной — наполовину отменить смысл.
    """
    from test_58_worker_body import FakeTelegram, _worker
    from test_112_map_reduce import _source
    from test_131_system_alerts import RecordingBot, _with_bot

    await _with_bot(db, user.id)
    source = await _source(
        db, user, public_id="src-why", channels=[("@alpha", None, 5, "искать")]
    )
    bot = RecordingBot()
    await _worker(
        db, telegram=FakeTelegram(fail=ValueError("Could not find the input entity"))
    ).poll_source(db, source, alert_sender=bot)

    assert bot.sent, "тревога не ушла"
    text = bot.sent[0]["text"]
    assert "input entity" in text or "ValueError" in text, (
        f"тревога не называет причину и отсылает в журнал: {text}"
    )
