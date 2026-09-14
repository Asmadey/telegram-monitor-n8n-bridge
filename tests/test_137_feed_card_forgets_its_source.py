"""Карточка ленты не помнит источник, из которого родилась (найдено владельцем).

Симптом со скриншота: у свежих записей ленты вместо логотипа канала серый
кружок с буквой, у записей постарше картинка на месте. Один и тот же канал
(«Finder.work», «AI Engineers Jobs») выглядит по-разному в соседних строках.

Задача 11.9 (`test_119`) закрывала ровно эту жалобу — и закрыла половину
дороги. `/api/feed` честно считает `avatar_chat_id` по `monitor_id` карточки,
`feed.js` честно его читает. Не хватает середины: **`monitor_id` в карточку
никто не пишет.** Колонку завела ревизия 0012 и один раз заполнила у
исторических строк (`m.chat_id = feed_items.chat_id`) — оттуда и берутся
карточки с аватаркой. Конвейер источника (11.4) кладёт `chat_id: None`, а
`monitor_id` не кладёт вовсе: у каждой новой карточки пусты оба поля.

Почему этого не увидел ни один тест: все они сеют `FeedItem(...,
monitor_id=source.id)` руками. Проверялось чтение поля, которое тест сам же и
записал; писателя не проверял никто. Поэтому здесь карточка не создаётся
руками ни разу — лента смотрится после настоящего прогона `poll_source`.

Второй пострадавший от той же дыры — переразбор. `_batch_from_feed` берёт
промпты по `item.monitor_id`: пусто → источник не найден → канал разбирается
ПУСТЫМ промптом извлечения и сводится пустым промптом оформления. Со стороны
владельца это «модель стала хуже отвечать» — ровно то, чего комментарий в
`_reanalyze` обещал не допустить.

И третье, найденное по дороге: тот же `_batch_from_feed` читает источник
`db.get(Monitor, item.monitor_id)` — без фильтра по владельцу. Пока поле
никто не писал, это ничем не грозило. Теперь оно настоящее, и правило
«запросы через TenantRepo» начинает значить.
"""

import json

import pytest
from conftest import act_as
from sqlalchemy import select
from test_58_worker_body import _worker
from test_112_map_reduce import THREE, ChannelTelegram, RecordingLLM, _source

from app.models import FeedItem

pytestmark = pytest.mark.asyncio


def _pipeline(db, *, llm=None):
    """Воркер с НАСТОЯЩИМ диспетчером: карточку пишет он, а не тест."""
    from app.services.dispatch import dispatch as real_dispatch

    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=real_dispatch)
    worker.llm = llm or RecordingLLM()
    return worker


async def _one_card(db) -> FeedItem:
    cards = list(await db.scalars(select(FeedItem)))
    # Страж пустоты: без карточки любая проверка ниже зелёная и бессмысленная.
    assert len(cards) == 1, f"прогон дал {len(cards)} карточек вместо одной"
    return cards[0]


async def test_a_card_born_of_a_poll_remembers_its_source(db, user):
    """Ядро дефекта: строку пишет конвейер, и он обязан назвать источник."""
    source = await _source(db, user, channels=[("@alpha", -1001, 20, "первый")])

    await _pipeline(db).poll_source(db, source)

    card = await _one_card(db)
    assert card.monitor_id == source.id, (
        "карточка не знает, из какого источника она вышла "
        f"({card.monitor_id!r}): аватарку рисовать не по чему, а переразбор "
        "потеряет промпты источника"
    )


async def test_the_avatar_survives_the_whole_road_from_poll_to_feed(
    anon_client, db, user
):
    """Жалоба владельца целиком: от прогона до того, что видно в ленте.

    `test_119` проверял этот же ответ на карточке, посеянной руками, — то
    есть проверял вторую половину дороги по данным, которых на первой не
    появляется.
    """
    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@alpha", -1001, 20, "первый")])

    await _pipeline(db).poll_source(db, source)

    card = (await anon_client.get("/api/feed")).json()["feed"][0]
    assert card.get("avatar_chat_id") == -1001, (
        "лента не знает, чью аватарку рисовать — в интерфейсе буква вместо "
        f"логотипа канала: {card.get('avatar_chat_id')!r}"
    )


async def test_reanalysis_of_a_fresh_card_keeps_the_prompts_of_the_source(db, user):
    """Второй пострадавший: «обновить разбор» на свежей карточке.

    Источник из одного канала сливает извлечение и оформление в один запрос
    (11.4), поэтому оба промпта обязаны быть видны в одном вызове.
    """
    source = await _source(
        db, user, channels=[("@alpha", -1001, 20, "извлекать продажи")]
    )
    worker = _pipeline(db)
    await worker.poll_source(db, source)
    card = await _one_card(db)

    llm = RecordingLLM()
    worker.llm = llm
    await worker._reanalyze(db, user.id, card.id)

    prompts = " | ".join(call["prompt"] for call in llm.calls)
    assert "извлекать продажи" in prompts, (
        f"переразбор пошёл без промпта извлечения канала: {prompts!r}"
    )
    assert "свести и оформить" in prompts, (
        f"переразбор пошёл без промпта оформления источника: {prompts!r}"
    )


async def test_prompts_are_never_read_from_another_tenants_source(db, user, user_b):
    """Источник карточки читается по владельцу, а не по голому ключу.

    Строка своя, `monitor_id` подложен на чужой источник — ровно тот случай,
    ради которого в проекте есть `TenantRepo`.
    """
    foreign = await _source(
        db,
        user_b,
        channels=[("@alpha", -1001, 20, "чужой промпт извлечения")],
        public_id="src-foreign",
        answer_prompt="чужой промпт оформления",
    )
    db.add(
        FeedItem(
            user_id=user.id,
            job_id="job-forged",
            monitor_id=foreign.id,
            chat_id=None,
            chat_title="Моя запись",
            messages_count=1,
            raw_messages_json=json.dumps(
                [{"id": 11, "text": "пост", "chat_id": -1001}], ensure_ascii=False
            ),
            ai_analysis="сводка",
        )
    )
    await db.commit()
    card = await _one_card(db)

    llm = RecordingLLM()
    worker = _pipeline(db, llm=llm)
    await worker._reanalyze(db, user.id, card.id)

    prompts = " | ".join(call["prompt"] for call in llm.calls)
    assert "чужой" not in prompts, (
        f"в разбор своей карточки уехали промпты чужого кабинета: {prompts!r}"
    )


async def test_migration_gives_the_source_back_to_cards_already_in_the_base(
    alembic_target_db,
):
    """Поведенческий уровень: живой Postgres, реальная ревизия 0017.

    Починенный писатель помогает только новым прогонам. У владельца в базе
    уже лежат карточки без источника — им связь возвращает ревизия, и
    возвращает по имени: конвейер кладёт в `chat_title` карточки заголовок
    ИСТОЧНИКА, а не канала.

    Проверяется и то, чего ревизия делать НЕ должна. Неверный `monitor_id`
    хуже пустого: он показал бы аватарку чужого канала и разобрал бы
    карточку чужими промптами, и отличить это от правильной работы уже
    нельзя.
    """
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0016_channel_token_limit")

    engine = create_async_engine(alembic_target_db)
    try:
        async with engine.begin() as conn:

            async def _user(email: str) -> int:
                await conn.execute(
                    text(
                        "INSERT INTO users (email, is_admin, timezone, "
                        "created_at, updated_at) "
                        "VALUES (:e, false, 'UTC', now(), now())"
                    ),
                    {"e": email},
                )
                return (
                    await conn.execute(
                        text("SELECT id FROM users WHERE email = :e"), {"e": email}
                    )
                ).scalar_one()

            async def _source(user_id: int, public_id: str, title: str) -> int:
                # Умолчания колонок питоновские (`default=`), а не серверные:
                # сырой SQL их не получает и обязан задать сам.
                await conn.execute(
                    text(
                        "INSERT INTO monitors (public_id, user_id, "
                        "interval_minutes, is_active, created_at, title, "
                        "answer_prompt, stop_words, running) "
                        "VALUES (:p, :u, 60, true, now(), :t, '', '', false)"
                    ),
                    {"p": public_id, "u": user_id, "t": title},
                )
                return (
                    await conn.execute(
                        text(
                            "SELECT id FROM monitors WHERE user_id = :u "
                            "AND public_id = :p"
                        ),
                        {"u": user_id, "p": public_id},
                    )
                ).scalar_one()

            async def _card(user_id: int, job_id: str, title: str, chat_id=None):
                await conn.execute(
                    text(
                        "INSERT INTO feed_items (user_id, job_id, created_at, "
                        "messages_count, chat_title, chat_id, monitor_id) "
                        "VALUES (:u, :j, now(), 1, :t, :c, NULL)"
                    ),
                    {"u": user_id, "j": job_id, "t": title, "c": chat_id},
                )

            owner = await _user("owner-0017@example.com")
            other = await _user("other-0017@example.com")

            single = await _source(owner, "src-single", "ТОП-вакансии")
            await _source(owner, "src-twin-a", "Двойное имя")
            await _source(owner, "src-twin-b", "Двойное имя")
            await _source(other, "src-other", "ТОП-вакансии")

            await _card(owner, "job-single", "ТОП-вакансии")
            await _card(owner, "job-twin", "Двойное имя")
            await _card(owner, "job-plain", "AI Engineers Jobs", chat_id=-100777)
            await _card(other, "job-other", "Чужое имя")

        command.upgrade(cfg, "head")

        async with engine.connect() as conn:
            rows = dict(
                (
                    await conn.execute(
                        text("SELECT job_id, monitor_id FROM feed_items")
                    )
                ).all()
            )

        assert rows["job-single"] == single, (
            "карточка так и не нашла свой источник — в ленте останется буква "
            f"вместо логотипа: {rows['job-single']!r}"
        )
        assert rows["job-twin"] is None, (
            "имя в кабинете принадлежит двум источникам, а связь всё равно "
            "проставлена — половина таких карточек показывает чужой канал"
        )
        assert rows["job-plain"] is None, (
            "тронута запись с собственным чатом: в ней `chat_title` — имя "
            "КАНАЛА, и совпадение с именем источника ничего не значит"
        )
        assert rows["job-other"] is None, (
            "источник подобран не тому владельцу — утечка через ленту"
        )

        # Идемпотентность: ревизия применяется один раз, но прогон миграций
        # повторяется при каждой выкладке.
        command.upgrade(cfg, "head")
    finally:
        await engine.dispose()
