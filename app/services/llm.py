"""LLM-анализ с лимитами расходов (задача 4.5 PLAN.md).

Оригинал (server.py:795) шлёт ПОЛНЫЕ тексты постов без потолка: канал с
длинными постами и интервалом 15 минут — неограниченный счёт. Три защиты:

1. Потолок символов на запрос (`truncate_posts`): батч обрезается по
   хвост (голова — свежие посты — обязана уйти), а не уходит целиком.
2. Месячный счётчик токенов на тенанта (`llm_usage`, период YYYY-MM),
   списание — атомарный upsert (воркер + ручной запуск одновременно).
3. Гейт и автоотключение: лимит уже превышен → к API НЕ обращаемся,
   None + запись в журнал; превышен этим запросом → токены списаны,
   openrouter_enabled = False, журнал.

Вызывающий API инъекцируется (`caller`): тесты офлайн; дефолтный
`openrouter_caller` — httpx c Bearer-ключом (расшифрованным из
Integration, 3.4) и usage из ответа (для списания). Ошибки API — в
журнал (порт try/except оригинала); текст исключения пойдёт через
redact в задаче 4.6.
"""

import datetime
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.models import Integration, LLMUsage
from app.services.integrations import integration_secrets
from app.services.journal import add_log

MAX_REQUEST_CHARS = 48_000
MONTHLY_TOKEN_LIMIT = 2_000_000
DEFAULT_SYSTEM_PROMPT = (
    "Выдели ключевую суть сообщений, ключевые технологии, условия и теги. Будь краток."
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _period(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m")


def truncate_posts(
    messages: list[dict], *, limit: int = MAX_REQUEST_CHARS
) -> list[dict]:
    """Порт post_items оригинала + потолок: суммарный текст постов не
    больше limit; режем ХВОСТ (следующие посты уже не влезают)."""
    items: list[dict] = []
    remaining = limit
    for item in messages:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        items.append(
            {
                "ID": str(item.get("id", "")),
                "пост": text,
                "ссылка": item.get("post_url", "")
                or f"https://t.me/{item.get('chat_username', 'c')}/{item.get('id', '')}",
            }
        )
        remaining -= len(text)
    return items


async def monthly_tokens_used(
    db, user_id: int, *, now: datetime.datetime | None = None
) -> int:
    """Расход тенанта за ТЕКУЩИЙ месяц (прошлые месяцы не считаются)."""
    now = now or _utcnow()
    result = await db.execute(
        select(LLMUsage.tokens).where(
            LLMUsage.user_id == user_id, LLMUsage.period == _period(now)
        )
    )
    return result.scalar_one_or_none() or 0


async def _add_tokens(db, user_id: int, tokens: int, *, now: datetime.datetime) -> None:
    """Атомарное списание: upsert с инкрементом — конкурентные списания
    (воркер + ручной запуск) не теряют токены."""
    insert = sqlite.insert if db.bind.dialect.name == "sqlite" else postgresql.insert
    stmt = insert(LLMUsage).values(
        user_id=user_id, period=_period(now), tokens=tokens, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "period"],
        set_={"tokens": LLMUsage.__table__.c.tokens + stmt.excluded.tokens},
    )
    await db.execute(stmt)
    await db.commit()


async def _log_limit(db, user_id: int, *, disabled: bool) -> None:
    """Запись в журнал о лимите: без неё тенант не поймёт, почему AI молчит.
    Через add_log (4.6): единственная точка записи, redact внутри."""
    details = f"месячный лимит {MONTHLY_TOKEN_LIMIT} токенов превышен — " + (
        "AI отключён" if disabled else "запрос пропущен без обращения к API"
    )
    await add_log(db, user_id, "LLM_LIMIT", details, status="ERROR")
    logger.warning("тенант %s: %s", user_id, details)


async def process_messages_batch_with_llm(
    db,
    user_id: int,
    messages: list[dict],
    *,
    custom_prompt: str | None = None,
    caller=None,
    now: datetime.datetime | None = None,
) -> str | None:
    """Анализ батча с гейтами: выключенный AI → None; превышенный лимит →
    None БЕЗ обращения к API; пересечение лимита этим запросом → списание,
    автоотключение openrouter_enabled, журнал."""
    now = now or _utcnow()
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one_or_none()
    # порт гейта оригинала: is_enabled + api_key
    if integration is None or not integration.openrouter_enabled:
        return None
    api_key = integration_secrets(integration).get("openrouter_api_key", "").strip()
    if not api_key or not messages:
        return None

    items = truncate_posts(messages)
    if not items:
        return None

    if await monthly_tokens_used(db, user_id, now=now) >= MONTHLY_TOKEN_LIMIT:
        await _log_limit(db, user_id, disabled=False)
        return None

    if caller is None:
        caller = openrouter_caller(
            api_key=api_key,
            base_url=integration.openrouter_base_url,
            model=integration.openrouter_model,
        )

    effective_prompt = (custom_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    payload = {
        "model": integration.openrouter_model,
        "messages": [
            {"role": "system", "content": effective_prompt},
            {
                "role": "user",
                "content": json.dumps({"post": items}, ensure_ascii=False, indent=2),
            },
        ],
    }

    try:
        analysis, tokens = await caller(payload)
    except Exception as e:  # noqa: BLE001 — падение API не роняет воркера
        # через add_log (4.6): текст исключения несёт заголовки с Bearer —
        # redact затирает ДО записи
        await add_log(
            db,
            user_id,
            "OPENROUTER_ERROR",
            f"Ошибка обработки батча через LLM: {e}",
            status="ERROR",
        )
        logger.exception("ошибка OpenRouter для тенанта %s", user_id)
        return None

    if not analysis:
        return None
    await _add_tokens(db, user_id, tokens or 0, now=now)

    # пересечение лимита ЭТИМ запросом: токены уже списаны — отключаем
    if await monthly_tokens_used(db, user_id, now=now) >= MONTHLY_TOKEN_LIMIT:
        integration.openrouter_enabled = False
        await _log_limit(db, user_id, disabled=True)
    return analysis


def openrouter_caller(*, api_key: str, base_url: str, model: str, transport=None):
    """Дефолтный вызывающий OpenRouter: Bearer-ключ, /chat/completions;
    из ответа берутся и текст, и usage.total_tokens (для списания)."""

    async def caller(payload: dict) -> tuple[str, int]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://telegram-monitor.local",
            "X-Title": "Telegram MTProto Monitor",
        }
        async with httpx.AsyncClient(
            timeout=45.0, follow_redirects=False, transport=transport
        ) as client:
            resp = await client.post(
                f"{(base_url or 'https://openrouter.ai/api/v1').rstrip('/')}"
                "/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or [{}]
            content = choices[0].get("message", {}).get("content", "").strip()
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return content, tokens

    return caller
