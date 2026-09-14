"""Системная тревога тенанту через его же бота (задача 13.3).

`GET /api/ops/health` знает про базу, воркер и очередь, но это **опрос**:
чтобы узнать об отказе, надо зайти и посмотреть. Молчащий воркер снаружи
выглядит как «сегодня ничего не нашлось» — лента пуста, ошибок нигде нет.

Адресат выбран не новый: тот же бот, которым уходят сводки. Токен и чат уже
настроены и уже проверены живой кнопкой — заводить второй канал значило бы
завести второй способ его не настроить.

Четыре правила, каждое из которых можно нарушить и получить нечто худшее,
чем молчание:

1. **Выключатель сводок тревогу не гасит.** Он говорит «не шли мне
   дайджесты», а не «не говори, что сервис встал».
2. **Одна тревога — один раз в окно.** Источник падает по расписанию каждые
   полчаса; без окна бот станет шумом, а шум читают так же, как тишину.
   Окно хранится в журнале, а не в своей таблице: отдельная схема ради
   счётчика повторов — это миграция, риск выкладки и лишняя сущность.
3. **Отказ Bot API не роняет вызывающего.** Тревога — диагностика ПОВЕРХ
   отказа; упав, она удваивает отказ вместо того, чтобы о нём сообщить.
4. **В тексте нет секретов.** Он уходит наружу, в чужой мессенджер.
"""

import datetime
import logging

from app.db import TenantRepo
from app.models import Integration, LogEntry
from app.services.integrations import integration_secrets
from app.services.journal import add_log
from app.services.telegram_markup import escape as tg_escape

logger = logging.getLogger(__name__)

ALERT_EVENT = "SYSTEM_ALERT"
# Час — компромисс: отказ, который длится сутки, напомнит о себе 24 раза, а
# падающий каждые полчаса источник — не 48.
WINDOW_MINUTES = 60


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    """SQLite отдаёт время без зоны, Postgres — с зоной."""
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def _mark(key: str) -> str:
    return f"[{key}]"


async def _sent_recently(db, user_id: int, key: str, now, window_minutes: int) -> bool:
    since = now - datetime.timedelta(minutes=window_minutes)
    rows = await db.scalars(
        TenantRepo(db, user_id)
        .query(LogEntry)
        .where(LogEntry.event_type == ALERT_EVENT, LogEntry.status == "SENT")
        .order_by(LogEntry.id.desc())
        .limit(50)
    )
    for row in rows:
        if _mark(key) in (row.details or "") and _aware(row.timestamp) >= since:
            return True
    return False


async def alert_owner(
    db,
    user_id: int,
    *,
    key: str,
    text: str,
    sender=None,
    now: datetime.datetime | None = None,
    window_minutes: int = WINDOW_MINUTES,
) -> bool:
    """Отправить тревогу владельцу тенанта. True — ушло.

    `key` — не текст, а ИМЯ повода: по нему считается окно повторов. Один
    и тот же отказ должен приходить с одним ключом, разные — с разными.
    """
    now = now or _utcnow()

    integration = (await db.scalars(TenantRepo(db, user_id).query(Integration))).first()
    token = (
        integration_secrets(integration).get("telegram_bot_token", "").strip()
        if integration is not None
        else ""
    )
    chat_id = (integration.telegram_sender_id or "").strip() if integration else ""
    if not token or not chat_id:
        # Бот необязателен. Некому слать — это не отказ, и шуметь об этом в
        # журнале на каждый тик тоже незачем.
        return False

    if await _sent_recently(db, user_id, key, now, window_minutes):
        return False

    body = f"🛠 <b>Teleton</b>\n\n{tg_escape(text)}"
    try:
        from app.services.dispatch import send_telegram_bot_message

        await (sender or send_telegram_bot_message)(token, chat_id, body)
    except Exception as exc:  # noqa: BLE001 — тревога не роняет вызывающего
        # Отката здесь НЕТ, и это важнее, чем кажется: упал HTTP-запрос к Bot
        # API, а не база — откатывать нечего. Зато `rollback()` обесценил бы
        # ВСЮ сессию вызывающего (факт 4 CLAUDE.md), и воркер, позвавший
        # тревогу из своего цикла, получил бы `MissingGreenlet` на следующем
        # обращении к собственным объектам. Тревога обязана быть безобидной
        # для того, кто о ней просит.
        await add_log(
            db,
            user_id,
            ALERT_EVENT,
            f"{_mark(key)} тревогу отправить не удалось: {type(exc).__name__}",
            status="ERROR",
            timestamp=now,
        )
        logger.warning("тенант %s: тревога %s не ушла", user_id, key)
        return False

    await add_log(
        db, user_id, ALERT_EVENT, f"{_mark(key)} {text}", status="SENT", timestamp=now
    )
    return True
