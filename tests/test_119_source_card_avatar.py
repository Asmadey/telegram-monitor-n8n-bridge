"""Карточка источника в ленте показывает аватарку, а не букву (найдено владельцем).

На скриншоте ленты один и тот же канал «AI Engineers Jobs» выглядит по-разному:
запись шестичасовой давности несёт логотип канала, двухчасовая — серый кружок с
буквой «A». Разница не в канале, а в том, ЧЕМ запись создана. До фазы 11 карточку
писал опрос одного канала и клал в неё `chat_id`; фронтенд рисует аватарку как
`/api/avatars/{chat_id}` и без него честно откатывается на букву. Конвейер
источника кладёт `chat_id: None` — и это не небрежность: у источника каналов
много, одного `chat_id` у него нет.

Сами аватарки при этом на месте: `poll_source` их скачивает и складывает через
`store_avatar` по каждому каналу. Потерян не файл, а дорога к нему.

Решение: карточка источника несёт `avatar_chat_id` — канал источника с
наименьшим `position`. Для источника из одного канала это ровно его аватарка;
для нескольких — тот канал, который в сводке идёт первым, то есть порядок,
который владелец сам задал перетаскиванием (11.7). Поле отдельное, а не
дозаполненный `chat_id`: у записи действительно нет одного чата, и подменять
этим факт означало бы соврать детальному виду и дедупликации.
"""

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio


async def _source_with_channels(db, user, *, public_id: str, channels: list[tuple]):
    """channels: список (chat_id, title, position)."""
    from app.models import Monitor, MonitorChannel

    source = Monitor(user_id=user.id, public_id=public_id, title="Вакансии")
    db.add(source)
    await db.commit()
    for chat_id, title, position in channels:
        db.add(
            MonitorChannel(
                monitor_id=source.id,
                user_id=user.id,
                chat_target=f"@ch{abs(chat_id)}",
                chat_id=chat_id,
                chat_title=title,
                position=position,
                extract_prompt="искать",
            )
        )
    await db.commit()
    return source


async def _source_card(db, user, source, *, job_id="job-1"):
    from app.models import FeedItem

    db.add(
        FeedItem(
            user_id=user.id,
            job_id=job_id,
            monitor_id=source.id,
            chat_id=None,  # у источника нет одного чата — так пишет конвейер
            chat_title=source.title,
            messages_count=3,
            ai_analysis="сводка",
        )
    )
    await db.commit()


async def test_card_of_a_single_channel_source_carries_its_channel_avatar(
    anon_client, db, user
):
    """Главный случай владельца: источник из одного канала."""
    await act_as(anon_client, db, user)
    source = await _source_with_channels(
        db, user, public_id="src-1", channels=[(-1001, "AI Engineers Jobs", 0)]
    )
    await _source_card(db, user, source)

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert card.get("avatar_chat_id") == -1001, (
        "карточка источника не знает, чью аватарку рисовать — фронтенд "
        f"откатится на букву: {card.get('avatar_chat_id')!r}"
    )


async def test_several_channels_take_the_first_by_position(anon_client, db, user):
    """Порядок каналов задан владельцем и определяет порядок в сводке (11.7)."""
    await act_as(anon_client, db, user)
    source = await _source_with_channels(
        db,
        user,
        public_id="src-2",
        channels=[(-2002, "Второй", 1), (-1001, "Первый", 0), (-3003, "Третий", 2)],
    )
    await _source_card(db, user, source)

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert card.get("avatar_chat_id") == -1001, (
        "взят не первый по порядку канал — аватарка разойдётся с тем, что "
        "стоит первым в сводке"
    )


async def test_channel_without_a_resolved_chat_is_not_offered_as_avatar(
    anon_client, db, user
):
    """Канал добавлен, но ещё не разрешён: `chat_id` нулевой, аватарки нет."""
    await act_as(anon_client, db, user)
    source = await _source_with_channels(
        db, user, public_id="src-3", channels=[(0, "Ещё не опрошен", 0)]
    )
    await _source_card(db, user, source)

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert not card.get("avatar_chat_id"), (
        "предложен неразрешённый канал — /api/avatars/0 ответит 404, и вместо "
        "буквы пользователь увидит битую картинку"
    )


async def test_a_plain_card_keeps_its_own_chat(anon_client, db, user):
    """Запись с собственным чатом (до фазы 11) ничего не теряет."""
    from app.models import FeedItem

    await act_as(anon_client, db, user)
    db.add(
        FeedItem(
            user_id=user.id,
            job_id="job-old",
            chat_id=-7007,
            chat_title="Одиночный канал",
            messages_count=1,
            ai_analysis="сводка",
        )
    )
    await db.commit()

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert card["chat_id"] == -7007
    assert card.get("avatar_chat_id") == -7007, (
        "своя аватарка потеряна — поле должно работать для обоих видов записей"
    )


async def test_resolution_never_reaches_into_another_tenant(
    anon_client, db, user, user_b
):
    """Каналы чужого источника не подставляются в свою карточку."""
    from app.models import FeedItem

    foreign = await _source_with_channels(
        db, user_b, public_id="src-foreign", channels=[(-9009, "Чужой", 0)]
    )
    await act_as(anon_client, db, user)
    db.add(
        FeedItem(
            user_id=user.id,
            job_id="job-mine",
            monitor_id=foreign.id,  # подложено: строка своя, источник чужой
            chat_id=None,
            chat_title="Моя запись",
            messages_count=1,
            ai_analysis="сводка",
        )
    )
    await db.commit()

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert card.get("avatar_chat_id") != -9009, (
        "аватарка подтянулась из чужого кабинета — утечка через ленту"
    )


async def test_the_list_does_not_query_per_card(anon_client, db, user):
    """N+1: аватарки для полусотни карточек — не полсотни запросов."""
    from sqlalchemy import event

    from app.models import FeedItem

    await act_as(anon_client, db, user)
    for index in range(30):
        source = await _source_with_channels(
            db,
            user,
            public_id=f"src-n{index}",
            channels=[(-1000 - index, f"Канал {index}", 0)],
        )
        db.add(
            FeedItem(
                user_id=user.id,
                job_id=f"job-n{index}",
                monitor_id=source.id,
                chat_id=None,
                chat_title=source.title,
                messages_count=1,
                ai_analysis="сводка",
            )
        )
    await db.commit()

    statements: list[str] = []
    engine = db.get_bind()

    def _record(conn, cursor, statement, *rest):
        statements.append(statement)

    # В тестовой сборке bind — уже синхронный Engine (aiosqlite за ним),
    # атрибута `sync_engine` у него нет: он есть только у AsyncEngine.
    target = getattr(engine, "sync_engine", engine)
    event.listen(target, "before_cursor_execute", _record)
    try:
        response = await anon_client.get("/api/feed?limit=50")
    finally:
        event.remove(target, "before_cursor_execute", _record)

    assert response.status_code == 200
    channel_queries = [s for s in statements if "monitor_channels" in s]
    assert len(channel_queries) <= 1, (
        f"запрос за каналами на каждую карточку ({len(channel_queries)} шт.) — "
        "лента из 50 записей станет полусотней походов в базу"
    )


def test_the_interface_actually_reads_the_field_the_api_sends():
    """Стык API ↔ интерфейс. Ровно на таком стыке сломалась форма нового
    источника: разметку переименовали, правило стилей осталось на старом имени.
    Поле, которое сервер считает и отдаёт, но никто не читает, — та же дыра,
    только тихая: тесты API зелёные, а пользователь видит букву."""
    import pathlib

    js = (
        pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "feed.js"
    ).read_text(encoding="utf-8")
    assert "avatar_chat_id" in js, (
        "лента не читает avatar_chat_id — сервер считает аватарку источника "
        "впустую, пользователь по-прежнему видит букву"
    )
    assert js.count("/api/avatars/${item.chat_id}") == 0, (
        "остался прямой переход по chat_id: у записи источника он пуст"
    )


async def test_the_card_does_not_carry_internal_foreign_keys(anon_client, db, user):
    """Контракт 9.10 на карточке ленты — найдено ревью в тот же день.

    Первая версия `avatar_chat_id` заодно положила в ответ `monitor_id`:
    поле понадобилось внутри `_card`, и оно машинально уехало в список
    выдаваемых. Но `_card` берёт его с ORM-объекта, а не из словаря, — в
    ответе оно было не нужно вовсе. Это внутренний BIGINT чужой таблицы, и
    контракт 9.10 говорит ровно о нём: наружу уходит `public_id`.

    Собственный `id` строки ленты — исключение, оговорённое той же задачей:
    им интерфейс адресует карточку (`/api/feed/{id}`).
    """
    from app.models import Monitor

    await act_as(anon_client, db, user)
    source = await _source_with_channels(
        db, user, public_id="src-contract", channels=[(-1001, "Канал", 0)]
    )
    await _source_card(db, user, source)

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    leaked = [k for k in ("monitor_id", "user_id") if k in card]
    assert not leaked, (
        f"в ответ уехал внутренний ключ {leaked}: наружу ходит только public_id "
        "(контракт 9.10)"
    )
    assert await db.get(Monitor, source.id) is not None, "источник должен быть в базе"
