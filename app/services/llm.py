"""LLM-анализ с лимитами расходов (задача 4.5 PLAN.md).

Оригинал (server.py:795) шлёт ПОЛНЫЕ тексты постов без потолка: канал с
длинными постами и интервалом 15 минут — неограниченный счёт. Здесь:

1. Потолок символов на один запрос (`truncate_posts`). Надёжный путь
   воркера режет полный текст на несколько запросов и сохраняет прогресс
   после каждого, поэтому этот технический предел не теряет сообщения.
2. Месячный счётчик токенов на тенанта (`llm_usage`, период YYYY-MM) и его
   разрез по источнику и каналу (12.7), списание — атомарный upsert
   (воркер + ручной запуск одновременно).

Общего месячного потолка на тенанта больше нет (13.19, решение владельца):
ключ OpenRouter у каждого свой (9.5), счёт приходит пользователю, лимит
расхода он ставит в OpenRouter, а ограничивать расход внутри сервиса
позволяет потолок КАНАЛА (13.5) — его выбирает сам пользователь. Зашитые
2 млн токенов были единственным ограничением, которого он не выбирал: на
пределе AI молча выключался.

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
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from app.models import Integration, LLMUsage, LLMUsageSlice
from app.services.integrations import integration_secrets
from app.services.journal import add_log, redact

MAX_REQUEST_CHARS = 48_000
DEFAULT_SYSTEM_PROMPT = (
    "Выдели ключевую суть сообщений, ключевые технологии, условия и теги. Будь краток."
)

# Сведение читает не посты, а разборы каналов (13.24). Подавать их тем же
# кодом, что посты, значило выдать модели «пост» с выдуманной ссылкой
# `t.me/c/1` и без названия канала — и модель честно описывала один пост-
# дайджест, а получив HTML-документ, рецензировала «ваш пост». Что перед ней
# разбор, говорит сервис: промпт оформления пишет человек, и про устройство
# конвейера он знать не обязан.
REDUCE_PREAMBLE = (
    "Ниже — результаты разбора каналов, а не посты. Сведи их в одно сообщение "
    "по инструкции ниже. Сам разбор не оценивай и не комментируй: это материал "
    "для ответа, а не вопрос к тебе."
)
DEFAULT_REDUCE_PROMPT = "Сведи результаты разбора каналов в одно краткое сообщение."

logger = logging.getLogger(__name__)


def _item(message: dict, text: str, *, verdicts: bool) -> dict:
    """Один элемент запроса: пост со ссылкой или разбор под именем канала."""
    if verdicts:
        return {"канал": message.get("chat_title") or "", "разбор": text}
    return {
        "ID": str(message.get("id", "")),
        "пост": text,
        "ссылка": message.get("post_url", "")
        or f"https://t.me/{message.get('chat_username', 'c')}/{message.get('id', '')}",
    }


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _period(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m")


def truncate_posts(
    messages: list[dict], *, limit: int = MAX_REQUEST_CHARS, verdicts: bool = False
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
        items.append(_item(item, text, verdicts=verdicts))
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


async def channel_tokens_used(
    db,
    user_id: int,
    *,
    source_public_id: str,
    chat_id: int,
    now: datetime.datetime | None = None,
) -> int:
    """Расход ОДНОГО канала источника за текущий период (13.5).

    Считается по разрезу 12.7, а не по общему счётчику: общий отвечает на
    вопрос «сколько всего», а потолок канала — на вопрос «сколько сжёг этот».
    """
    now = now or _utcnow()
    value = await db.scalar(
        select(LLMUsageSlice.tokens).where(
            LLMUsageSlice.user_id == user_id,
            LLMUsageSlice.period == _period(now),
            LLMUsageSlice.source_public_id == (source_public_id or ""),
            LLMUsageSlice.chat_id == (chat_id or 0),
        )
    )
    return int(value or 0)


async def _add_tokens(
    db,
    user_id: int,
    tokens: int,
    *,
    now: datetime.datetime,
    commit: bool = True,
    source_public_id: str = "",
    chat_id: int = 0,
    chat_title: str = "",
) -> None:
    """Атомарное списание: upsert с инкрементом — конкурентные списания
    (воркер + ручной запуск) не теряют токены.

    Пишет ДВА счётчика в одной транзакции: общий по тенанту и разрез по
    источнику и каналу (задача 12.7; на разрезе держится потолок канала 13.5). Вместе, а не
    по отдельности, — иначе сумма по разрезу однажды разойдётся с расходом, и
    отчёт, по которому решают, какой канал отключить, начнёт врать.
    """
    insert = sqlite.insert if db.bind.dialect.name == "sqlite" else postgresql.insert
    stmt = insert(LLMUsage).values(
        user_id=user_id, period=_period(now), tokens=tokens, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "period"],
        set_={"tokens": LLMUsage.__table__.c.tokens + stmt.excluded.tokens},
    )
    await db.execute(stmt)

    sliced = insert(LLMUsageSlice).values(
        user_id=user_id,
        period=_period(now),
        source_public_id=source_public_id or "",
        chat_id=chat_id or 0,
        chat_title=chat_title or "",
        tokens=tokens,
        updated_at=now,
    )
    sliced = sliced.on_conflict_do_update(
        index_elements=["user_id", "period", "source_public_id", "chat_id"],
        set_={
            "tokens": LLMUsageSlice.__table__.c.tokens + sliced.excluded.tokens,
            # имя канала могли переименовать — храним последнее известное,
            # но не затираем его пустотой из вызова, который имени не знает
            "chat_title": func.coalesce(
                func.nullif(sliced.excluded.chat_title, ""),
                LLMUsageSlice.__table__.c.chat_title,
            ),
            "updated_at": sliced.excluded.updated_at,
        },
    )
    await db.execute(sliced)
    if commit:
        await db.commit()


async def process_messages_batch_with_llm(
    db,
    user_id: int,
    messages: list[dict],
    *,
    custom_prompt: str | None = None,
    caller=None,
    now: datetime.datetime | None = None,
    require_success: bool = False,
    completed: list[str] | None = None,
    checkpoint=None,
    source_public_id: str = "",
    chat_id: int = 0,
    chat_title: str = "",
    schema: dict | None = None,
    verdicts: bool = False,
) -> str | None:
    """Анализ батча с гейтами и устойчивыми checkpoints.

    Старые прямые вызовы сохраняют прежний контракт с одним ограниченным
    запросом. `require_success=True` используется durable job: весь текст
    уходит чанками, успешные чанки не повторяются.

    `source_public_id` / `chat_id` / `chat_title` — адрес расхода (12.7).
    Пустая строка и ноль означают «вне источника» и «не канал»: прямой
    разбор из ленты и сведение по каналам тоже тратят токены, но канала
    у них нет.

    `verdicts=True` — сведение (13.24): на входе разборы каналов
    (`chat_title` + `text`), и модель получает их под именами каналов, а не
    постами со ссылками.
    """
    now = now or _utcnow()
    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_id))
    ).scalar_one_or_none()
    # порт гейта оригинала: is_enabled + api_key
    if integration is None or not integration.openrouter_enabled:
        return None
    api_key = integration_secrets(integration).get("openrouter_api_key", "").strip()
    if not api_key or not messages:
        if require_success and not api_key:
            raise RuntimeError("LLM enabled without API key")
        return None

    items = truncate_posts(messages, verdicts=verdicts)
    batches = [items]
    if require_success:
        # Technical request size, not a quota: every character is processed.
        batches = []
        current: list[dict] = []
        size = 0
        for message in messages:
            text = message.get("text") or ""
            for offset in range(0, len(text), MAX_REQUEST_CHARS):
                part = text[offset : offset + MAX_REQUEST_CHARS]
                if current and size + len(part) > MAX_REQUEST_CHARS:
                    batches.append(current)
                    current, size = [], 0
                current.append(_item(message, part, verdicts=verdicts))
                size += len(part)
        if current:
            batches.append(current)
        items = batches[0] if batches else []
    if not items:
        return None

    if caller is None:
        caller = openrouter_caller(
            api_key=api_key,
            base_url=integration.openrouter_base_url,
            model=integration.openrouter_model,
        )

    effective_prompt = (custom_prompt or "").strip() or (
        DEFAULT_REDUCE_PROMPT if verdicts else DEFAULT_SYSTEM_PROMPT
    )
    if verdicts:
        effective_prompt = f"{REDUCE_PREAMBLE}\n\n{effective_prompt}"
    key = "каналы" if verdicts else "post"
    payload: dict = {
        "model": integration.openrouter_model,
        "messages": [
            {"role": "system", "content": effective_prompt},
            {
                "role": "user",
                "content": json.dumps({key: items}, ensure_ascii=False, indent=2),
            },
        ],
    }

    if schema is not None:
        # Строгий режим: провайдер САМ не даст модели ответить мимо схемы —
        # ни обёрткой в ограждение, ни пояснением, ни оборванной структурой.
        # `require_parameters` обязателен: без него OpenRouter спокойно уводит
        # запрос к провайдеру, который параметр молча игнорирует, и строгость
        # существует только на бумаге.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "findings", "strict": True, "schema": schema},
        }
        payload["provider"] = {"require_parameters": True}

    async def ask(batch_payload: dict):
        """Запрос к модели с одним запасным ходом: без схемы.

        Структурный вывод умеют не все модели OpenRouter, а модель выбирает
        пользователь. Без этого запасного хода смена модели в настройках
        молча убила бы все источники — отказ приходил бы на каждом канале.

        Повторяется ТОЛЬКО отказ про `response_format`: нехватка денег,
        лимиты и падения провайдера остаются отказами.
        """
        try:
            return await caller(batch_payload)
        except httpx.HTTPStatusError as exc:
            unsupported = (
                "response_format" in batch_payload
                and exc.response is not None
                and exc.response.status_code in (400, 404, 422)
                and "response_format" in (exc.response.text or "")
            )
            if not unsupported:
                raise
            logger.info("модель не умеет json_schema — повтор без неё")
            retry = dict(batch_payload)
            retry.pop("response_format", None)
            retry.pop("provider", None)
            return await caller(retry)

    try:
        analyses = list(completed or [])
        tokens = 0
        for batch in batches[len(analyses) :]:
            payload["messages"][1]["content"] = json.dumps(
                {key: batch}, ensure_ascii=False, indent=2
            )
            result, used = await ask(payload)
            if require_success and not result:
                raise RuntimeError("LLM returned empty analysis")
            if result:
                analyses.append(result)
            if require_success:
                await _add_tokens(
                    db,
                    user_id,
                    used or 0,
                    now=now,
                    commit=checkpoint is None,
                    source_public_id=source_public_id,
                    chat_id=chat_id,
                    chat_title=chat_title,
                )
                if checkpoint is not None:
                    await checkpoint(analyses)
            else:
                tokens += used or 0
        analysis = "\n\n".join(analyses)
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
        logger.warning("ошибка OpenRouter для тенанта %s: %s", user_id, redact(str(e)))
        if require_success:
            raise RuntimeError(
                "LLM analysis failed; original messages retained for retry"
            ) from None
        return None

    if not analysis:
        return None
    if not require_success:
        await _add_tokens(
            db,
            user_id,
            tokens or 0,
            now=now,
            source_public_id=source_public_id,
            chat_id=chat_id,
            chat_title=chat_title,
        )
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
