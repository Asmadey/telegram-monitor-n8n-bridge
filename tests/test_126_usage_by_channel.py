"""Расход токенов в разрезе источника и канала (задача 12.7 `docs/PLAN.md`).

`llm_usage` считает на тенанта, а решение принимается **на канал**: лимит
сообщений и промпт извлечения задаются каналу, и «дорого» — это всегда про
конкретный канал. При счётчике на пользователя видно только, что бюджет тает;
какой из десяти каналов его жжёт — нет. Отключать приходится наугад.

Разрез сделан отдельной таблицей, а не колонками в `llm_usage`, и это не
вкусовое решение. Месячный гейт читает ОДНУ строку на (тенант, период)
через `scalar_one_or_none`; добавь в ту же таблицу строки по каналам —
и он упадёт на второй. Счётчик, на котором держится защита от неограниченного
счёта, ломать ради отчёта нельзя.

Ключ разреза — `source_public_id`, а не внутренний BIGINT источника. Две
причины, и обе важнее удобства: наружу внутренние ключи не отдаются
(контракт 9.10), а расход обязан пережить удаление источника — иначе история
трат исчезает вместе с тем, ради чего её вели.

Главное свойство, которое здесь закрепляется: **сумма по разрезу сходится с
расходом тенанта.** Разрез, который не сходится с общим счётчиком, хуже
отсутствия разреза: по нему принимают решения, а он врёт.
"""

import datetime

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import Base, Integration
from app.services.integrations import save_integration_secrets

NOW = datetime.datetime(2026, 9, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
PERIOD = "2026-09"
TABLE = "llm_usage_slices"


def _msgs(n: int = 2) -> list[dict]:
    return [
        {"id": i, "text": f"пост {i}", "post_url": f"https://t.me/c/{i}"}
        for i in range(1, n + 1)
    ]


def _caller(tokens: int):
    async def call(payload):
        return "вердикт", tokens

    return call


async def _enable_ai(db, user_id: int) -> Integration:
    await save_integration_secrets(db, user_id, openrouter_api_key="sk-or-test-key")
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one()
    integration.openrouter_enabled = True
    await db.commit()
    return integration


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------


def test_the_breakdown_table_exists():
    assert TABLE in Base.metadata.tables, (
        f"нет таблицы {TABLE}: расход считается только на тенанта, и какой "
        "канал жжёт бюджет — неизвестно"
    )


def test_the_breakdown_is_tenant_scoped():
    table = Base.metadata.tables[TABLE]
    assert "user_id" in table.c and not table.c.user_id.nullable
    assert any("user_id" in index.columns for index in table.indexes), (
        "user_id без индекса: фильтр по тенанту выродится в полный проход"
    )


def test_the_breakdown_is_keyed_by_period_source_and_channel():
    table = Base.metadata.tables[TABLE]
    keys = {
        tuple(sorted(c.name for c in constraint.columns))
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_id", "period", "source_public_id", "user_id") in keys, (
        "нет ключа (тенант, период, источник, канал): без него инкремент "
        f"превращается в россыпь строк — {keys}"
    )


def test_the_breakdown_outlives_the_source_it_describes():
    """Источник удалят, а история трат обязана остаться.

    Внешний ключ на `monitors` означал бы либо каскад (история исчезает
    вместе с источником), либо отказ удаления. Обе беды лечатся тем, что
    разрез хранит публичный идентификатор, а не ссылку.
    """
    table = Base.metadata.tables[TABLE]
    targets = {fk.column.table.name for fk in table.foreign_keys}
    assert targets <= {"users"}, (
        f"разрез ссылается на {targets - {'users'}}: удаление источника унесёт историю"
    )
    assert "source_public_id" in table.c, "разрез не хранит публичный id источника"


# --------------------------------------------------------------------------
# Поведение
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spend_lands_on_the_channel_that_caused_it(db, user):
    from app.models import LLMUsageSlice
    from app.services.llm import process_messages_batch_with_llm

    await _enable_ai(db, user.id)
    await process_messages_batch_with_llm(
        db,
        user.id,
        _msgs(),
        caller=_caller(120),
        now=NOW,
        source_public_id="src-1",
        chat_id=-100500,
        chat_title="Вакансии AI",
    )
    row = (await db.scalars(select(LLMUsageSlice))).one()
    assert (row.source_public_id, row.chat_id, row.tokens) == ("src-1", -100500, 120)
    assert row.period == PERIOD
    assert row.chat_title == "Вакансии AI", "разрез без имени канала нечитаем"


@pytest.mark.asyncio
async def test_channels_of_one_source_are_counted_apart(db, user):
    from app.models import LLMUsageSlice
    from app.services.llm import process_messages_batch_with_llm

    await _enable_ai(db, user.id)
    for chat_id, tokens in ((-1, 100), (-2, 300), (-1, 50)):
        await process_messages_batch_with_llm(
            db,
            user.id,
            _msgs(),
            caller=_caller(tokens),
            now=NOW,
            source_public_id="src-1",
            chat_id=chat_id,
        )
    rows = {
        r.chat_id: r.tokens for r in (await db.scalars(select(LLMUsageSlice))).all()
    }
    assert rows == {-1: 150, -2: 300}, (
        f"каналы не разделены или инкремент не накапливается: {rows}"
    )


@pytest.mark.asyncio
async def test_the_reduce_step_belongs_to_the_source_not_a_channel(db, user):
    """Сведение тратит токены на источник целиком — канала у него нет."""
    from app.models import LLMUsageSlice
    from app.services.llm import process_messages_batch_with_llm

    await _enable_ai(db, user.id)
    await process_messages_batch_with_llm(
        db, user.id, _msgs(), caller=_caller(70), now=NOW, source_public_id="src-1"
    )
    row = (await db.scalars(select(LLMUsageSlice))).one()
    assert row.chat_id == 0 and row.source_public_id == "src-1"


@pytest.mark.asyncio
async def test_the_sum_over_the_breakdown_matches_the_tenant_total(db, user):
    """Главное свойство: разрез, не сходящийся с общим счётчиком, врёт.

    По нему принимают решение, какой канал отключить, — значит расхождение
    стоит дороже, чем отсутствие разреза вовсе.
    """
    from app.models import LLMUsageSlice
    from app.services.llm import monthly_tokens_used, process_messages_batch_with_llm

    await _enable_ai(db, user.id)
    for chat_id, tokens in ((-1, 100), (-2, 300), (0, 70)):
        await process_messages_batch_with_llm(
            db,
            user.id,
            _msgs(),
            caller=_caller(tokens),
            now=NOW,
            source_public_id="src-1",
            chat_id=chat_id,
        )
    # сюда же — расход вне источника (переразбор из ленты)
    await process_messages_batch_with_llm(
        db, user.id, _msgs(), caller=_caller(25), now=NOW
    )

    total = await monthly_tokens_used(db, user.id, now=NOW)
    by_slice = await db.scalar(select(func.sum(LLMUsageSlice.tokens)))
    assert total == 495, f"общий счётчик изменил поведение: {total}"
    assert by_slice == total, f"разрез {by_slice} не сходится с расходом {total}"


@pytest.mark.asyncio
async def test_one_tenant_does_not_see_another_tenants_breakdown(db, user_a, user_b):
    from app.models import LLMUsageSlice
    from app.services.llm import process_messages_batch_with_llm

    for owner, tokens in ((user_a, 100), (user_b, 900)):
        await _enable_ai(db, owner.id)
        await process_messages_batch_with_llm(
            db,
            owner.id,
            _msgs(),
            caller=_caller(tokens),
            now=NOW,
            source_public_id="src-1",
            chat_id=-7,
        )
    mine = (
        await db.scalars(
            select(LLMUsageSlice).where(LLMUsageSlice.user_id == user_a.id)
        )
    ).all()
    assert [r.tokens for r in mine] == [100], (
        "разрез смешал тенантов: один и тот же канал могут читать разные клиенты"
    )


@pytest.mark.asyncio
async def test_a_source_run_attributes_spend_channel_by_channel(db, user):
    """Сквозная проверка: прогон источника, а не вызов службы напрямую.

    Половины стыка проверять по отдельности мало. Служба умеет записать
    адрес расхода — это выше; воркер обязан его ПЕРЕДАТЬ, и ошибка здесь
    выглядела бы как исправно работающий отчёт, в котором всё свалено в
    «вне источника». Поэтому двойник подменяет только обращение к модели
    (`llm_caller`), а учёт идёт настоящий.
    """
    from test_58_worker_body import _worker
    from test_112_map_reduce import ChannelTelegram, _post, _source

    from app.models import LLMUsageSlice
    from app.services.llm import monthly_tokens_used

    await _enable_ai(db, user.id)
    source = await _source(
        db,
        user,
        public_id="src-42",
        channels=[("@alpha", -1001, 5, "искать А"), ("@beta", -1002, 5, "искать Б")],
    )
    telegram = ChannelTelegram(
        {-1001: ("@alpha", [_post(11)]), -1002: ("@beta", [_post(21)])}
    )

    async def caller(payload):
        return "вердикт", 40

    await _worker(db, telegram=telegram).poll_source(db, source, llm_caller=caller)

    rows = {
        (r.source_public_id, r.chat_id): r.tokens
        for r in (await db.scalars(select(LLMUsageSlice))).all()
    }
    assert rows == {
        ("src-42", -1001): 40,
        ("src-42", -1002): 40,
        ("src-42", 0): 40,  # сведение по каналам
    }, f"расход разнесён не по каналам источника: {rows}"
    assert sum(rows.values()) == await monthly_tokens_used(db, user.id), (
        "сумма разреза разошлась с расходом тенанта"
    )


# --------------------------------------------------------------------------
# Показ: данные, до которых нельзя дойти, решения не меняют
# --------------------------------------------------------------------------


async def _spend(db, user_id: int, rows: list[tuple[str, int, str, int]]) -> None:
    """Посев расхода напрямую: здесь проверяется показ, а не учёт."""
    from app.services.llm import _add_tokens

    for source_public_id, chat_id, chat_title, tokens in rows:
        await _add_tokens(
            db,
            user_id,
            tokens,
            now=NOW,
            source_public_id=source_public_id,
            chat_id=chat_id,
            chat_title=chat_title,
        )


@pytest.mark.asyncio
async def test_usage_endpoint_groups_spend_by_source_and_channel(anon_client, db, user):
    await act_as(anon_client, db, user)
    from app.models import Monitor

    db.add(Monitor(user_id=user.id, public_id="src-1", title="Вакансии AI"))
    await db.commit()
    await _spend(
        db,
        user.id,
        [
            ("src-1", -1, "alpha", 100),
            ("src-1", -2, "beta", 300),
            ("src-1", 0, "", 70),
            ("", 0, "", 25),
        ],
    )

    response = await anon_client.get("/api/usage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 495
    assert body["outside_sources"] == 25
    source = body["sources"][0]
    assert source["public_id"] == "src-1"
    assert source["title"] == "Вакансии AI"
    assert source["tokens"] == 470, "расход источника считается вместе со сведением"
    assert source["summary_tokens"] == 70
    assert [(c["chat_title"], c["tokens"]) for c in source["channels"]] == [
        ("beta", 300),
        ("alpha", 100),
    ], "каналы идут не по убыванию расхода — вопрос «кто жжёт бюджет» без ответа"


@pytest.mark.asyncio
async def test_usage_endpoint_survives_a_deleted_source(anon_client, db, user):
    """История трат переживает источник — иначе её незачем было вести."""
    await act_as(anon_client, db, user)
    await _spend(db, user.id, [("src-ушёл", -1, "alpha", 10)])
    body = (await anon_client.get("/api/usage")).json()
    assert body["sources"][0]["public_id"] == "src-ушёл"
    assert body["sources"][0]["title"], "у исчезнувшего источника нет даже подписи"


@pytest.mark.asyncio
async def test_usage_endpoint_never_returns_internal_keys(anon_client, db, user):
    """Контракт 9.10: наружу идёт public_id, внутренние BIGINT — нет."""
    await act_as(anon_client, db, user)
    from app.models import Monitor

    db.add(Monitor(user_id=user.id, public_id="src-1", title="Вакансии AI"))
    await db.commit()
    await _spend(db, user.id, [("src-1", -1, "alpha", 10)])
    response = await anon_client.get("/api/usage")
    # без этой строки проверка зеленеет на 404: в теле «Not Found» ключей тоже нет
    assert response.status_code == 200, response.text
    raw = response.text
    assert '"monitor_id"' not in raw and '"user_id"' not in raw, raw
    assert '"id"' not in raw, f"в ответе есть внутренний ключ: {raw}"


@pytest.mark.asyncio
async def test_usage_endpoint_is_closed_to_anonymous(anon_client):
    assert (await anon_client.get("/api/usage")).status_code == 401


@pytest.mark.asyncio
async def test_usage_endpoint_shows_only_your_own_spend(anon_client, db, user, user_b):
    await act_as(anon_client, db, user)
    await _spend(db, user.id, [("src-1", -1, "мой", 10)])
    await _spend(db, user_b.id, [("src-9", -2, "чужой", 900)])
    body = (await anon_client.get("/api/usage")).json()
    assert body["total"] == 10, f"в отчёт попал чужой расход: {body}"
    assert "чужой" not in (await anon_client.get("/api/usage")).text
