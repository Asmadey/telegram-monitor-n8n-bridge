"""Общего месячного потолка токенов больше нет (13.19, решение владельца).

Потолок 2 000 000 токенов на тенанта завела задача 4.5, когда у монолита не
было никакого ограничения расхода, а ключ OpenRouter был один на всех. С тех
пор ключ у каждого свой (9.5) — счёт приходит самому пользователю, и лимит
расхода он ставит в OpenRouter, — расход виден по каждому каналу (12.7, 12.10),
а у каждого канала есть свой потолок (13.5). Зашитое в код число без
возможности его поменять осталось единственным ограничением, которое
пользователь не выбирал: на пределе AI молча выключался или батч откладывался
до следующего месяца.

Что остаётся и сторожится здесь же:
- счётчик расхода и его разрез — по ним виден «какой канал жжёт бюджет»;
- потолок канала — единственный потолок, и он задаётся пользователем;
- технический предел размера запроса (`MAX_REQUEST_CHARS`) — это не квота.
"""

import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.sources import ChannelCreate, ChannelUpdate
from app.models import Integration, LLMUsage, LogEntry
from app.services.integrations import save_integration_secrets
from app.services.llm import monthly_tokens_used, process_messages_batch_with_llm

NOW = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
PERIOD = "2026-10"
# прежний зашитый потолок: расход выше него больше ничего не меняет
OLD_CAP = 2_000_000
# колонка `monitor_channels.token_limit` — Integer: больше сохранить нельзя
INT32_MAX = 2_147_483_647


def _msgs(n: int = 2) -> list[dict]:
    return [
        {"id": i, "text": f"пост {i}", "post_url": f"https://t.me/c/{i}"}
        for i in range(1, n + 1)
    ]


async def _enable_ai(db, user_id: int) -> None:
    await save_integration_secrets(db, user_id, openrouter_api_key="sk-or-test-key")
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one()
    integration.openrouter_enabled = True
    await db.commit()


def _caller(calls: list):
    async def caller(payload: dict) -> tuple[str, int]:
        calls.append(payload)
        return "суть постов", 500

    return caller


@pytest.mark.asyncio
async def test_spend_above_the_old_cap_still_reaches_the_model(db, _env, user_a):
    await _enable_ai(db, user_a.id)
    db.add(LLMUsage(user_id=user_a.id, period=PERIOD, tokens=OLD_CAP))
    await db.commit()

    calls: list = []
    result = await process_messages_batch_with_llm(
        db, user_a.id, _msgs(), caller=_caller(calls), now=NOW
    )

    assert calls, "к модели не обратились: общий потолок всё ещё действует"
    assert result == "суть постов"
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_a.id))
    ).scalar_one()
    assert integration.openrouter_enabled is True, (
        "AI выключился сам — пользователь этого не выбирал"
    )
    limit_logs = (
        await db.scalars(
            select(LogEntry).where(
                LogEntry.user_id == user_a.id, LogEntry.event_type == "LLM_LIMIT"
            )
        )
    ).all()
    assert not limit_logs, "в журнале запись о месячном лимите, которого нет"
    # счётчик остаётся: по нему строится отчёт о расходе
    assert await monthly_tokens_used(db, user_a.id, now=NOW) == OLD_CAP + 500


@pytest.mark.asyncio
async def test_durable_batch_is_not_deferred_by_monthly_spend(db, _env, user_a):
    """Путь воркера: прежде батч откладывался до следующего месяца."""
    await _enable_ai(db, user_a.id)
    db.add(LLMUsage(user_id=user_a.id, period=PERIOD, tokens=OLD_CAP * 3))
    await db.commit()

    calls: list = []
    try:
        result = await process_messages_batch_with_llm(
            db,
            user_a.id,
            _msgs(),
            caller=_caller(calls),
            now=NOW,
            require_success=True,
        )
    except Exception as exc:  # noqa: BLE001 — отказ и есть предмет проверки
        result = f"отказ: {type(exc).__name__}"
    assert calls, f"батч не дошёл до модели из-за месячного расхода ({result})"
    assert result == "суть постов"


def test_channel_cap_is_not_bounded_by_the_old_monthly_cap():
    """Потолок канала был ограничен сверху общим: «канальный больше общего
    ничего не ограничивает». Общего нет — граница только у типа колонки."""

    def accepted(model, **fields) -> bool:
        try:
            model(**fields)
        except ValidationError:
            return False
        return True

    assert accepted(ChannelCreate, chat_target="@x", token_limit=OLD_CAP * 5), (
        "потолок канала выше прежнего общего отклоняется"
    )
    assert accepted(ChannelUpdate, token_limit=OLD_CAP * 5), (
        "правка потолка канала выше прежнего общего отклоняется"
    )
    with pytest.raises(ValidationError):
        ChannelCreate(chat_target="@x", token_limit=INT32_MAX + 1)
    with pytest.raises(ValidationError):
        ChannelCreate(chat_target="@x", token_limit=-1)
