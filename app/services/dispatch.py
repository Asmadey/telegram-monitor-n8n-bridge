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
from sqlalchemy import update

from app.db import TenantRepo
from app.models import ChatAvatar, FeedItem, Integration, SentMessage
from app.services.integrations import integration_secrets
from app.services.journal import add_log
from app.services.llm import process_messages_batch_with_llm
from app.services.telegram_markup import escape as tg_escape
from app.services.telegram_markup import rich_html_to_plain, to_telegram_html
from app.services.webhook import send_webhook

logger = logging.getLogger(__name__)

# Telegram режет сообщение на 4096 символов; 3900 — запас оригинала под
# HTML-разметку, которая в лимит входит вместе с текстом
BOT_CHUNK = 3900
# У rich-сообщения лимит 32768; запас на случай, если клиент считает иначе
RICH_CHUNK = 30000
BOT_TIMEOUT = 15.0
# сколько постов показать в текстовой сводке, когда анализа нет
PREVIEW_POSTS = 5
PREVIEW_CHARS = 250

MODEL_DIRECT = "MTProto Direct"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def send_telegram_bot_message(
    token: str, chat_id: str, text: str, *, transport=None
) -> bool:
    """Отправка сводки: сначала богатым методом, затем прежним (9.19).

    `sendRichMessage` (Bot API 10.1) знает таблицы, заголовки и списки и
    держит 32768 символов вместо 4096. Именно этого не хватало: модель
    отвечает Markdown-таблицей, а `sendMessage` таблиц не знает вовсе — и
    сводка на пятнадцать вакансий приходила стеной из чёрточек.
    `parse_mode` с ним не передаётся: форматирование лежит внутри
    `rich_message`.

    Запасной путь обязателен, но включается ТОЛЬКО если не ушло ещё ничего.
    Иначе отказ на втором куске означал бы, что первый уже доставлен, а
    следом уедет вся сводка целиком — человек получит её дважды.

    Повтор без `parse_mode` на запасном пути сохранён: заголовки каналов
    пишет не сервис, и несбалансированный тег в чужом названии — это 400 на
    каждой доставке, а не разовый сбой.
    """
    base = f"https://api.telegram.org/bot{token}"
    async with httpx.AsyncClient(timeout=BOT_TIMEOUT, transport=transport) as client:
        delivered = 0
        for chunk in _chunks(text, RICH_CHUNK):
            response = await client.post(
                f"{base}/sendRichMessage",
                json={"chat_id": chat_id, "rich_message": {"html": chunk}},
            )
            if response.status_code != 200:
                if delivered:
                    response.raise_for_status()
                break
            delivered += 1
        else:
            return True

        plain = rich_html_to_plain(text)
        for chunk in _chunks(plain, BOT_CHUNK):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
            response = await client.post(f"{base}/sendMessage", json=payload)
            if response.status_code != 200:
                payload.pop("parse_mode", None)
                response = await client.post(f"{base}/sendMessage", json=payload)
            response.raise_for_status()
        return True


def _summary_text(chat_title: str, messages: list[dict]) -> str:
    """Текстовая сводка, когда анализа нет (порт server.py:920).

    Заголовок канала и тексты постов пишем не мы: знак `<` в чужом посте
    ломал разбор, Bot API отвечал 400, доставка повторяла запрос без
    parse_mode — и сообщение уходило целиком без оформления (9.17).
    Экранируем ровно чужое: собственная разметка сводки остаётся разметкой.
    """
    lines = [f"📢 <b>Новые посты: {tg_escape(chat_title)}</b> ({len(messages)} шт.)\n"]
    for message in messages[:PREVIEW_POSTS]:
        text = tg_escape((message.get("text") or "")[:PREVIEW_CHARS])
        url = message.get("post_url", "")
        link = f" — <a href='{tg_escape(url)}'>🔗 Источник</a>" if url else ""
        lines.append(f"• {text}{link}\n")
    return "\n".join(lines)


def _chunks(text: str, limit: int = BOT_CHUNK) -> list[str]:
    """Резать по длине, но не посреди тега.

    Слепая нарезка каждые 3900 символов однажды разрубает тег пополам:
    Bot API отвечает 400, доставка повторяет запрос без parse_mode, и
    оформление пропадает у всего сообщения. Граница отступает назад — к
    началу незакрытого тега, а по возможности к концу строки.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = limit
        window = rest[:cut]
        if window.rfind("<") > window.rfind(">"):
            cut = window.rfind("<")
        newline = rest.rfind("\n", 0, cut)
        if newline > cut - 500:
            cut = newline + 1
        cut = max(cut, 1)
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


async def _integration(db, user_id: int) -> Integration | None:
    return (await db.scalars(TenantRepo(db, user_id).query(Integration))).first()


async def _run_bot(
    db, user_id, integration, payload, messages, analysis, sender
) -> str:
    if integration is None or not integration.telegram_forward_enabled:
        return "skipped"
    token = integration_secrets(integration).get("telegram_bot_token", "").strip()
    chat_id = (integration.telegram_sender_id or "").strip()
    # гейт оригинала: пустой токен или чат — тихий отказ БЕЗ запроса
    if not token or not chat_id:
        return "failed"

    chat_title = payload.get("chat_title") or "Источник"
    # Формат ответа модели зависит от промпта канала, а промпты пишет
    # владелец: у одного канала HTML, у другого Markdown, у третьего
    # промпта нет вовсе. Приводим к разметке Bot API здесь — доставка не
    # вправе рассчитывать на дисциплину модели (9.17).
    text = (
        to_telegram_html(analysis) if analysis else _summary_text(chat_title, messages)
    )
    try:
        # Нарезка переехала внутрь отправителя: у богатого метода свой
        # лимит (32768), у запасного свой (4096), и снаружи выбирать нечем.
        # Имя публичное (8.3): ту же отправку переиспользует живая проверка
        # бота в app/api/checks.py. Отказ Bot API — False, а не исключение:
        # без этой ветки сбой доставки выглядел бы SUCCESS (9.1).
        sent = await (sender or send_telegram_bot_message)(token, chat_id, text)
        if sent is False:
            raise RuntimeError("Telegram bot rejected delivery")
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
        return "failed"

    await add_log(
        db,
        user_id,
        "TG_BOT_SENT",
        f"Отправлено сообщение в Telegram ботом для «{chat_title}» "
        f"(AI-анализ: {'да' if analysis else 'нет'})",
        status="SUCCESS",
        chat_title=chat_title,
    )
    return "sent"


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


async def _mark_processed(db, repo, payload: dict, messages: list[dict]) -> None:
    """Пометить разобранные посты обработанными.

    Чат берётся у самого поста, а не у батча: в сводке источника посты
    приходят из разных каналов, и общего `chat_id` у батча нет.
    """
    by_chat: dict[int, list[int]] = {}
    for message in messages:
        if not message.get("id"):
            continue
        chat_id = message.get("chat_id") or payload.get("chat_id")
        if chat_id is None:
            continue
        by_chat.setdefault(int(chat_id), []).append(message["id"])
    for chat_id, ids in by_chat.items():
        await db.execute(
            update(SentMessage)
            .where(
                SentMessage.id.in_(
                    repo.query(SentMessage).with_only_columns(SentMessage.id)
                ),
                SentMessage.chat_id == chat_id,
                SentMessage.message_id.in_(ids),
            )
            .values(processed=True)
        )


async def dispatch(
    db,
    user_id: int,
    payload: dict,
    *,
    channel_prompt: str | None = None,
    analysis: str | None = None,
    llm_caller=None,
    bot_sender=None,
    webhook_sender=None,
) -> dict:
    """Провести выборку через доставку и записать её в ленту тенанта.

    `analysis` передаёт конвейер источника (11.4): разбор уже сделан по
    каналам и сведён, повторять его здесь нечего. Пустая строка при этом
    значит «совпадений нет» — карточка в ленту пишется, а бот и вебхук
    молчат. Это разные вещи: тишина здесь осмысленная, а не отказ.
    """
    messages = payload.get("messages") or []
    if not messages:
        return {"status": "no_messages"}

    integration = await _integration(db, user_id)

    # job_id is the durable batch identity shared by retries and n8n dedup.
    batch_id = payload.get("job_id") or str(uuid.uuid4())
    payload["job_id"] = batch_id
    repo = TenantRepo(db, user_id)
    item = (
        await db.scalars(repo.query(FeedItem).where(FeedItem.job_id == batch_id))
    ).first()
    if item is None:
        item = FeedItem(
            user_id=user_id,
            job_id=batch_id,
            chat_id=payload.get("chat_id"),
            chat_title=payload.get("chat_title"),
            chat_username=payload.get("chat_username") or "",
            messages_count=len(messages),
            ai_analysis="",
            raw_messages_json=json.dumps(messages, ensure_ascii=False),
            model_name=(
                integration.openrouter_model
                if integration is not None and integration.openrouter_enabled
                else MODEL_DIRECT
            ),
            delivery_status="ANALYZING",
            analysis_progress_json="[]",
            bot_status="pending",
            webhook_status="pending",
        )
        db.add(item)
        await db.commit()
    if analysis is not None and item.delivery_status == "ANALYZING":
        # Разбор пришёл готовым: конвейер уже спросил модель по каналам
        item.ai_analysis = analysis
        item.delivery_status = "PENDING"
        await _mark_processed(db, repo, payload, messages)
        await db.commit()
    elif item.delivery_status == "ANALYZING":

        async def checkpoint(analyses):
            item.analysis_progress_json = json.dumps(analyses, ensure_ascii=False)
            await db.commit()

        analysis = await process_messages_batch_with_llm(
            db,
            user_id,
            messages,
            custom_prompt=channel_prompt,
            caller=llm_caller,
            require_success=True,
            completed=json.loads(item.analysis_progress_json or "[]"),
            checkpoint=checkpoint,
        )
        item.ai_analysis = analysis or ""
        item.delivery_status = "PENDING"
        await _mark_processed(db, repo, payload, messages)
        # The result and processed markers become durable BEFORE sending.
        await db.commit()
        if analysis:
            await add_log(
                db,
                user_id,
                "AI_ANALYSIS",
                f"Сгенерирован AI-анализ ({len(analysis)} симв.)",
                status="SUCCESS",
                chat_id=payload.get("chat_id"),
                chat_title=payload.get("chat_title"),
            )
    silent = analysis is not None and not analysis
    analysis = item.ai_analysis or ""
    if analysis:
        payload["ai_analysis"] = analysis
    if silent:
        # Совпадений нет — говорить нечего. Карточка в ленте остаётся, и по
        # ней видно, что прогон был: тишина не должна выглядеть как отказ.
        item.bot_status = "skipped"
        item.webhook_status = "skipped"
        item.delivery_status = "NO_MATCHES"
        await db.commit()
        return {"status": "no_matches", "job_id": batch_id}
    if item.bot_status not in ("sent", "skipped"):
        item.bot_status = await _run_bot(
            db, user_id, integration, payload, messages, analysis, bot_sender
        )
        await db.commit()
    if item.webhook_status not in ("sent", "skipped"):
        item.webhook_status = await _run_webhook(
            db, user_id, integration, payload, messages, webhook_sender
        )
        await db.commit()
    retry = "failed" in (item.bot_status, item.webhook_status)
    item.delivery_status = "ERROR" if retry else "SUCCESS"
    await db.commit()
    return {
        "status": "dispatched",
        "ai": bool(analysis),
        "bot_sent": item.bot_status == "sent",
        "webhook": item.webhook_status,
        "feed_item_id": item.id,
        "retry": retry,
    }
