"""Потолок расхода на канал (задача 13.5).

Задача 12.7 дала разрез: видно, какой канал жжёт бюджет. Ограничить его было
нечем — потолок один на тенанта, и когда он достигнут, AI выключается
целиком. То есть увидеть виновника можно, а наказать — только вместе со всеми.

Правило простое и целиком в этом: **канал, исчерпавший свой потолок, не
разбирается; остальные каналы источника работают дальше.** Иначе потолок
превращается в тот же общий рубильник, только с лишним полем в форме.

Ноль — «без потолка»: значение по умолчанию, и смысл его ровно такой. Колонка
`NOT NULL DEFAULT 0` вместо `NULL` — чтобы «не задано» и «ноль токенов» не
выглядели одинаково в запросах.
"""

import datetime

import pytest
from sqlalchemy import select
from test_58_worker_body import _worker
from test_112_map_reduce import ChannelTelegram, RecordingLLM, _post, _source

from app.models import Base, Integration, LogEntry, MonitorChannel
from app.services.integrations import save_integration_secrets
from app.services.llm import _add_tokens

pytestmark = pytest.mark.asyncio

TWO = {-1001: ("@alpha", [_post(11)]), -1002: ("@beta", [_post(21)])}


async def _enable_ai(db, user_id: int) -> None:
    await save_integration_secrets(db, user_id, openrouter_api_key="sk-or-test")
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one()
    integration.openrouter_enabled = True
    await db.commit()


async def _spend(db, user_id: int, source: str, chat_id: int, tokens: int) -> None:
    await _add_tokens(
        db,
        user_id,
        tokens,
        now=datetime.datetime.now(datetime.timezone.utc),
        source_public_id=source,
        chat_id=chat_id,
    )


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------


def test_the_channel_carries_its_own_ceiling():
    column = Base.metadata.tables["monitor_channels"].c
    assert "token_limit" in column, "у канала нет собственного потолка расхода"
    assert not column.token_limit.nullable, (
        "потолок допускает NULL: «не задано» и «ноль токенов» станут неразличимы"
    )


# --------------------------------------------------------------------------
# Поведение прогона
# --------------------------------------------------------------------------


async def test_a_channel_over_its_ceiling_is_skipped(db, user):
    await _enable_ai(db, user.id)
    source = await _source(
        db,
        user,
        public_id="src-cap",
        channels=[("@alpha", -1001, 5, "искать А"), ("@beta", -1002, 5, "искать Б")],
    )
    channels = list(
        await db.scalars(
            select(MonitorChannel).where(MonitorChannel.monitor_id == source.id)
        )
    )
    for channel in channels:
        if channel.chat_id == -1001:
            channel.token_limit = 100
    await db.commit()
    await _spend(db, user.id, "src-cap", -1001, 150)

    llm = RecordingLLM()
    worker = _worker(db, telegram=ChannelTelegram(TWO))
    worker.llm = llm
    await worker.poll_source(db, source)

    prompts = [call["prompt"] for call in llm.calls]
    assert not any("искать А" in p for p in prompts), (
        f"канал сверх своего потолка всё равно ушёл в модель: {prompts}"
    )
    assert any("искать Б" in p for p in prompts), (
        f"потолок одного канала выключил остальные — это общий рубильник: {prompts}"
    )


async def test_a_channel_below_its_ceiling_still_works(db, user):
    """Антивакуум: заслон, отсекающий всё, «проходит» тест выше."""
    await _enable_ai(db, user.id)
    source = await _source(
        db, user, public_id="src-ok", channels=[("@alpha", -1001, 5, "искать А")]
    )
    channels = list(
        await db.scalars(
            select(MonitorChannel).where(MonitorChannel.monitor_id == source.id)
        )
    )
    channels[0].token_limit = 10_000
    await db.commit()
    await _spend(db, user.id, "src-ok", -1001, 150)

    llm = RecordingLLM()
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [_post(11)])}))
    worker.llm = llm
    await worker.poll_source(db, source)

    assert any("искать А" in c["prompt"] for c in llm.calls), (
        "канал с незакрытым потолком не разобран"
    )


async def test_zero_means_no_ceiling(db, user):
    await _enable_ai(db, user.id)
    source = await _source(
        db, user, public_id="src-free", channels=[("@alpha", -1001, 5, "искать А")]
    )
    await _spend(db, user.id, "src-free", -1001, 10_000_000)

    llm = RecordingLLM()
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [_post(11)])}))
    worker.llm = llm
    await worker.poll_source(db, source)

    assert llm.calls, "ноль понят как «нисколько», а не «без потолка»"


async def test_the_skipped_channel_is_named(db, user):
    """Молча пропущенный канал неотличим от «в нём ничего не нашлось»."""
    await _enable_ai(db, user.id)
    source = await _source(
        db, user, public_id="src-say", channels=[("@alpha", -1001, 5, "искать А")]
    )
    channels = list(
        await db.scalars(
            select(MonitorChannel).where(MonitorChannel.monitor_id == source.id)
        )
    )
    channels[0].token_limit = 100
    await db.commit()
    await _spend(db, user.id, "src-say", -1001, 150)

    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [_post(11)])}))
    worker.llm = RecordingLLM()
    await worker.poll_source(db, source)

    details = list(
        await db.scalars(select(LogEntry.details).where(LogEntry.user_id == user.id))
    )
    text = "\n".join(d or "" for d in details).lower()
    assert "потол" in text or "лимит" in text, (
        f"пропуск по потолку нигде не назван: {text[:400]}"
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


async def test_the_ceiling_is_set_and_returned_through_the_api(anon_client, db, user):
    from conftest import act_as

    await act_as(anon_client, db, user)
    created = await anon_client.post(
        "/api/sources", json={"title": "Источник", "interval_minutes": 60}
    )
    source_id = created.json()["public_id"]

    added = await anon_client.post(
        f"/api/sources/{source_id}/channels",
        json={"chat_target": "@alpha", "token_limit": 5000},
    )
    assert added.status_code in (200, 201), added.text

    listed = (await anon_client.get("/api/sources")).json()
    channel = listed["sources"][0]["channels"][0]
    assert channel["token_limit"] == 5000, f"потолок не доехал до интерфейса: {channel}"


async def test_the_ceiling_has_a_field_in_the_source_editor():
    """Потолок, который не выставить из кабинета, выставляет только агент."""
    import pathlib

    module = (
        pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "sources.js"
    ).read_text(encoding="utf-8")
    assert "channel-token-limit" in module, "в строке канала нет поля потолка"
    assert "token_limit" in module, "потолок не уходит на сервер при сохранении"
    assert "|| 0;" not in module.split("rawCap")[1][:200], (
        "ноль затирается через `|| 0` — а ноль здесь законное значение «без потолка»"
    )
