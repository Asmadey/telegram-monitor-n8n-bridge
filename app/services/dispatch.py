"""Диспетчеризация выборки: AI → Telegram-бот → n8n → лента.

Порт `process_and_dispatch_messages` (server.py:882) в разрезе тенанта.
Оригинал читал глобальные настройки функцией `get_setting`; здесь всё
берётся из строки `integrations` пользователя, а `user_id` приходит
параметром — в воркере он берётся ИЗ СТРОКИ ЗАДАЧИ, а не из сессии.

**Дефект оригинала, исправленный портом.** Шаг «отправка в n8n» при успехе
делал `return` (server.py:965), и шаг записи в ленту не выполнялся. Лента
наполнялась только у тех, у кого вебхук выключен или падает: с работающим
n8n история в интерфейсе оставалась пустой. Лента — журнал выполнения, а не
запасной путь доставки, поэтому здесь она пишется всегда.

Порядок шагов сохранён (AI до бота — боту нужен готовый анализ), гейты
портированы дословно: выключенная интеграция и пустой секрет одинаково
означают «не трогать», причём БЕЗ обращения к сети.

Все исходящие вызовы инъектируются. По умолчанию вебхук уходит через
`send_webhook` — то есть через проверку SSRF по резолвнутому IP: адрес мог
быть сохранён до появления валидации или перенесён скриптом миграции, и
проверка только на сохранении закрывала бы одну дверь из двух.
"""

import datetime
import json
import logging
import uuid

import httpx
from sqlalchemy import select

from app.models import ChatAvatar, FeedItem, Integration
from app.services.integrations import integration_secrets
from app.services.journal import add_log
from app.services.llm import process_messages_batch_with_llm
from app.services.webhook import send_webhook

logger = logging.getLogger(__name__)

# Telegram режет сообщение на 4096 символов; 3900 — запас оригинала под
# HTML-разметку, которая в лимит входит вместе с текстом
BOT_CHUNK = 3900
BOT_TIMEOUT = 15.0
# сколько постов показать в текстовой сводке, когда анализа нет
PREVIEW_POSTS = 5
PREVIEW_CHARS = 250

MODEL_DIRECT = "MTProto Direct"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def _default_bot_sender(token: str, chat_id: str, text: str) -> bool:
    """Порт server.py:854: HTML, при ошибке разметки — повтор без parse_mode.

    Пользователь пишет заголовки каналов, а не мы: несбалансированный тег в
    названии канала — это 400 от Bot API на КАЖДОЙ доставке, а не разовый сбой.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    async with httpx.AsyncClient(timeout=BOT_TIMEOUT) as client:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            payload.pop("parse_mode", None)
            response = await client.post(url, json=payload)
        response.raise_for_status()
        return True


def _summary_text(chat_title: str, messages: list[dict]) -> str:
    """Текстовая сводка, когда анализа нет (порт server.py:920)."""
    lines = [f"📢 <b>Новые посты: {chat_title}</b> ({len(messages)} шт.)\n"]
    for message in messages[:PREVIEW_POSTS]:
        text = (message.get("text") or "")[:PREVIEW_CHARS]
        url = message.get("post_url", "")
        link = f" — <a href='{url}'>🔗 Источник</a>" if url else ""
        lines.append(f"• {text}{link}\n")
    return "\n".join(lines)


def _chunks(text: str) -> list[str]:
    return [text[i : i + BOT_CHUNK] for i in range(0, len(text), BOT_CHUNK)] or [text]


async def _integration(db, user_id: int) -> Integration | None:
    return (
        await db.scalars(select(Integration).where(Integration.user_id == user_id))
    ).first()


async def _run_bot(
    db, user_id, integration, payload, messages, analysis, sender
) -> bool:
    if integration is None or not integration.telegram_forward_enabled:
        return False
    token = integration_secrets(integration).get("telegram_bot_token", "").strip()
    chat_id = (integration.telegram_sender_id or "").strip()
    # гейт оригинала: пустой токен или чат — тихий отказ БЕЗ запроса
    if not token or not chat_id:
        return False

    chat_title = payload.get("chat_title") or "Источник"
    text = analysis or _summary_text(chat_title, messages)
    try:
        for chunk in _chunks(text):
            await (sender or _default_bot_sender)(token, chat_id, chunk)
    except Exception as exc:  # noqa: BLE001 — доставка не роняет опрос
        await add_log(
            db,
            user_id,
            "TG_BOT_ERROR",
            f"Ошибка отправки через Telegram Bot API: {exc}",
            status="ERROR",
            chat_title=chat_title,
        )
        logger.warning("тенант %s: бот не отправил сообщение", user_id)
        return False

    await add_log(
        db,
        user_id,
        "TG_BOT_SENT",
        f"Отправлено сообщение в Telegram ботом для «{chat_title}» "
        f"(AI-анализ: {'да' if analysis else 'нет'})",
        status="SUCCESS",
        chat_title=chat_title,
    )
    return True


async def _run_webhook(db, user_id, integration, payload, messages, sender) -> str:
    """Возвращает 'sent' | 'failed' | 'skipped'."""
    if integration is None or not integration.auto_webhook_enabled:
        return "skipped"
    url = integration_secrets(integration).get("webhook_url", "").strip()
    if not url:
        return "skipped"

    body = {
        "source": "telethon_monitor",
        "event": "telegram_messages_batch",
        "timestamp": _utcnow().isoformat(),
        **payload,
    }
    try:
        await (sender or send_webhook)(url, body)
    except Exception as exc:  # noqa: BLE001 — n8n лежит, опрос продолжается
        await add_log(
            db,
            user_id,
            "WEBHOOK_ERROR",
            f"Ошибка отправки вебхука в n8n: {exc}",
            status="ERROR",
            chat_id=payload.get("chat_id"),
            chat_title=payload.get("chat_title"),
        )
        logger.warning("тенант %s: вебхук не отправлен", user_id)
        return "failed"

    await add_log(
        db,
        user_id,
        "WEBHOOK_SENT",
        f"Отправлен вебхук в n8n ({len(messages)} постов)",
        status="SUCCESS",
        chat_id=payload.get("chat_id"),
        chat_title=payload.get("chat_title"),
        messages_count=len(messages),
    )
    return "sent"


async def store_avatar(db, chat_id: int, image_bytes: bytes) -> None:
    """Аватарка канала (5.4): одна строка на канал, без user_id.

    Единственная точка записи `chat_avatars`. До неё таблица только
    читалась эндпоинтом ленты — то есть аватарки не появлялись никогда.
    """
    if not chat_id or not image_bytes:
        return
    avatar = await db.get(ChatAvatar, chat_id)
    if avatar is None:
        db.add(ChatAvatar(chat_id=chat_id, image_bytes=image_bytes))
    else:
        avatar.image_bytes = image_bytes
        avatar.fetched_at = _utcnow()
    await db.commit()


async def dispatch(
    db,
    user_id: int,
    payload: dict,
    *,
    channel_prompt: str | None = None,
    llm_caller=None,
    bot_sender=None,
    webhook_sender=None,
) -> dict:
    """Провести выборку через доставку и записать её в ленту тенанта."""
    messages = payload.get("messages") or []
    if not messages:
        return {"status": "no_messages"}

    integration = await _integration(db, user_id)

    # 1. AI. Гейты (выключен / нет ключа / исчерпан лимит) — внутри llm.py,
    # включая автоотключение при пересечении месячного потолка.
    analysis = await process_messages_batch_with_llm(
        db, user_id, messages, custom_prompt=channel_prompt, caller=llm_caller
    )
    if analysis:
        payload["ai_analysis"] = analysis
        await add_log(
            db,
            user_id,
            "AI_ANALYSIS",
            f"Сгенерирован AI-анализ ({len(analysis)} симв.): {analysis[:250]}",
            status="SUCCESS",
            chat_id=payload.get("chat_id"),
            chat_title=payload.get("chat_title"),
        )

    # 2. Пересылка ботом, 3. вебхук — независимы: падение одного не отменяет
    # другого и не отменяет ленту (дефект оригинала — см. модуль-докстринг)
    bot_sent = await _run_bot(
        db, user_id, integration, payload, messages, analysis, bot_sender
    )
    webhook = await _run_webhook(
        db, user_id, integration, payload, messages, webhook_sender
    )

    # 4. Лента — ВСЕГДА
    model_name = (
        integration.openrouter_model
        if (integration is not None and integration.openrouter_enabled)
        else MODEL_DIRECT
    )
    item = FeedItem(
        user_id=user_id,
        # полный uuid, а не первые 8 символов оригинала: job_id уникален
        # глобально, и на 8 hex-символах коллизии начинаются на десятках
        # тысяч строк — то есть чужая запись ломала бы вставку
        job_id=str(uuid.uuid4()),
        chat_id=payload.get("chat_id"),
        chat_title=payload.get("chat_title"),
        chat_username=payload.get("chat_username") or "",
        messages_count=len(messages),
        ai_analysis=analysis or "",
        raw_messages_json=json.dumps(messages, ensure_ascii=False),
        model_name=model_name,
        delivery_status="ERROR" if webhook == "failed" else "SUCCESS",
    )
    db.add(item)
    await db.commit()

    return {
        "status": "dispatched",
        "ai": bool(analysis),
        "bot_sent": bot_sent,
        "webhook": webhook,
        "feed_item_id": item.id,
    }
