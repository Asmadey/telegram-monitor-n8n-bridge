"""Задача 4.5 — лимиты расходов на LLM.

Оригинал (server.py:795) шлёт ПОЛНЫЕ тексты постов в OpenRouter без
потолка: канал с длинными постами и интервалом 15 минут —
неограниченный счёт. Три защиты:

1. Потолок символов на запрос — батч обрезается, а не уходит целиком.
2. Месячный счётчик токенов на тенанта (llm_usage, период YYYY-MM).
3. Автоотключение AI (openrouter_enabled → False) при превышении
   лимита с записью в журнал; при превышении анализ НЕ обращается к
   API — возвращает None.

API-вызовы инъекцируются (caller): тесты офлайн, OpenRouter не трогаем.
Шифрование секрета требует APP_ENCRYPTION_KEY — фикстура _env.
"""

import datetime
import json

import pytest
from sqlalchemy import select

from app.models import Integration, LLMUsage, LogEntry
from app.services.integrations import save_integration_secrets
from app.services.llm import (
    MAX_REQUEST_CHARS,
    MONTHLY_TOKEN_LIMIT,
    monthly_tokens_used,
    openrouter_caller,
    process_messages_batch_with_llm,
    truncate_posts,
)

NOW = datetime.datetime(2026, 9, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
PERIOD = "2026-09"


def _msgs(n: int = 3, text_len: int = 200) -> list[dict]:
    return [
        {
            "id": i,
            "text": f"пост {i} " + "x" * text_len,
            "post_url": f"https://t.me/c/{i}",
        }
        for i in range(1, n + 1)
    ]


async def _enable_ai(
    db, user_id: int, *, api_key: str = "sk-or-test-key"
) -> Integration:
    """Включить AI тенанту так же, как это сделает роутер Фазы 5."""
    await save_integration_secrets(db, user_id, openrouter_api_key=api_key)
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one()
    integration.openrouter_enabled = True
    await db.commit()
    return integration


async def _seed_usage(db, user_id: int, tokens: int, period: str = PERIOD) -> None:
    db.add(LLMUsage(user_id=user_id, period=period, tokens=tokens))
    await db.commit()


def _caller_log(calls: list):
    """Фейковый OpenRouter: пишет payload'ы, возвращает (анализ, токены)."""

    async def caller(payload: dict) -> tuple[str, int]:
        calls.append(payload)
        return "суть постов", 500

    return caller


# --- 1. потолок символов на запрос ---


def test_truncate_posts_caps_total_text():
    """Батч длиннее потолка обрезается: суммарный текст постов НЕ больше
    лимита, начало сохранено (режем хвост, а не голову)."""
    msgs = _msgs(n=50, text_len=MAX_REQUEST_CHARS // 10)
    items = truncate_posts(msgs)
    total = sum(len(item["пост"]) for item in items)
    assert total <= MAX_REQUEST_CHARS, (
        f"в запрос ушло {total} символов при потолке {MAX_REQUEST_CHARS}"
    )
    assert items, "батч обрезался до пустоты"
    # режем хвост: первый пост обязан уйти целиком
    assert len(items[0]["пост"]) == len(msgs[0]["text"].strip())


def test_truncate_posts_keeps_short_batch_intact():
    """Короткий батч не калечим: всё уходит как есть."""
    msgs = _msgs(n=2, text_len=100)
    items = truncate_posts(msgs)
    assert len(items) == 2
    assert items[0]["пост"] == msgs[0]["text"].strip()


# --- 2. месячный счётчик ---


@pytest.mark.asyncio
async def test_monthly_usage_scoped_by_user_and_period(db, user_a, user_b):
    """Счёт тенанта видит ТОЛЬКО его период и ТОЛЬКО его строки."""
    await _seed_usage(db, user_a.id, 1000, period=PERIOD)
    await _seed_usage(db, user_a.id, 4000, period="2026-08")  # прошлый месяц
    await _seed_usage(db, user_b.id, 9000, period=PERIOD)  # чужой тенант

    assert await monthly_tokens_used(db, user_a.id, now=NOW) == 1000, (
        "счётчик зацепил чужой месяц или чужого тенанта"
    )


@pytest.mark.asyncio
async def test_process_over_limit_returns_none_without_api(db, _env, user_a):
    """ГЛАВНЫЙ контракт: месячный лимит превышен → анализ None, к API НЕ
    обращаемся, в журнале — запись о лимите."""
    await _enable_ai(db, user_a.id)
    await _seed_usage(db, user_a.id, MONTHLY_TOKEN_LIMIT)

    calls: list = []
    result = await process_messages_batch_with_llm(
        db, user_a.id, _msgs(), caller=_caller_log(calls), now=NOW
    )
    assert result is None, "при превышении лимита анализ обратился к API"
    assert calls == [], "запрос к OpenRouter ушёл при превышенном лимите"

    logged = (
        await db.execute(select(LogEntry).where(LogEntry.user_id == user_a.id))
    ).scalars()
    assert any("лимит" in (e.details or "").lower() for e in logged), (
        "превышение лимита не записано в журнал"
    )


@pytest.mark.asyncio
async def test_process_disables_ai_when_limit_crossed(db, _env, user_a):
    """Лимит превышен ПОСЛЕ этого запроса → AI тенанта отключается и
    пишется журнал (автоотключение, план 4.5)."""
    await _enable_ai(db, user_a.id)
    await _seed_usage(db, user_a.id, MONTHLY_TOKEN_LIMIT - 100)

    calls: list = []
    result = await process_messages_batch_with_llm(
        db, user_a.id, _msgs(), caller=_caller_log(calls), now=NOW
    )
    assert result == "суть постов", "запрос до лимита не выполнен"
    assert calls, "к API не обратились при неисчерпанном лимите"

    assert await monthly_tokens_used(db, user_a.id, now=NOW) >= MONTHLY_TOKEN_LIMIT, (
        "израсходованные токены не записаны в счётчик"
    )
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_a.id))
    ).scalar_one()
    assert integration.openrouter_enabled is False, (
        "AI не отключился при превышении месячного лимита"
    )
    logged = (
        await db.execute(select(LogEntry).where(LogEntry.user_id == user_a.id))
    ).scalars()
    assert any("лимит" in (e.details or "").lower() for e in logged)


@pytest.mark.asyncio
async def test_process_under_limit_records_tokens_and_stays_enabled(db, _env, user_a):
    """Обычный путь: анализ есть, токены списаны, AI остался включён."""
    await _enable_ai(db, user_a.id)

    calls: list = []
    result = await process_messages_batch_with_llm(
        db, user_a.id, _msgs(), caller=_caller_log(calls), now=NOW
    )
    assert result == "суть постов"
    assert len(calls) == 1, "к API обратились не один раз"
    used = await monthly_tokens_used(db, user_a.id, now=NOW)
    assert used == 500, f"списано {used} токенов вместо 500 из ответа API"

    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_a.id))
    ).scalar_one()
    assert integration.openrouter_enabled is True, "AI отключился без причины"

    # в payload уходит обрезанный батч: потолок действует и здесь
    user_content = calls[0]["messages"][1]["content"]
    items = json.loads(user_content)["post"]
    assert sum(len(i["пост"]) for i in items) <= MAX_REQUEST_CHARS


@pytest.mark.asyncio
async def test_process_disabled_integration_skips_api(db, _env, user_a):
    """AI выключен у тенанта → None без обращения к API (порт гейта
    оригинала: is_enabled + api_key)."""
    await save_integration_secrets(db, user_a.id, openrouter_api_key="sk-or-test-key")
    # openrouter_enabled остаётся False

    calls: list = []
    result = await process_messages_batch_with_llm(
        db, user_a.id, _msgs(), caller=_caller_log(calls), now=NOW
    )
    assert result is None
    assert calls == [], "к API обратились при выключенном AI"


# --- дефолтный вызывающий: ключ и usage парсятся корректно ---


@pytest.mark.asyncio
async def test_openrouter_caller_sends_bearer_and_parses_usage(_env):
    """Дефолтный caller: Bearer-ключ из секрета, /chat/completions,
    из ответа берутся и текст, и usage (для списания счётчика)."""
    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "анализ от API"}}],
                "usage": {"total_tokens": 4242},
            },
        )

    caller = openrouter_caller(
        api_key="sk-or-test-key",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-v4-flash",
        transport=httpx.MockTransport(handler),
    )
    analysis, tokens = await caller({"model": "x", "messages": []})
    assert analysis == "анализ от API"
    assert tokens == 4242, "usage из ответа потерян — счётчик не спишется"
    assert captured["auth"] == "Bearer sk-or-test-key"
    assert captured["url"].endswith("/chat/completions")
