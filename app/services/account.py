"""Забрать своё и уйти (задача 13.2).

Для этого сервиса удаление аккаунта — не вежливость, а **способ отозвать
доступ**: на сервере лежит ключ от живого Telegram-аккаунта.

Порядок важен. Сначала завершается сессия в самом Telegram, потом стираются
строки. Стереть строку — значит убрать доступ у СЕРВИСА, но сессия остаётся
в списке устройств, а любая уцелевшая копия строки (резервная копия базы!)
продолжает работать. `log_out` закрывает и это.

Недоступный Telegram при этом не держит человека в заложниках: право уйти не
зависит от того, отвечает ли чужой сервис. Неудача остаётся в логе ПРОЦЕССА —
журнал тенанта к этому моменту уже удаляется.
"""

import logging

from sqlalchemy import delete

from app.db import TenantRepo
from app.models import (
    FeedItem,
    Integration,
    Job,
    LegacyImportRow,
    LLMUsage,
    LLMUsageSlice,
    LogEntry,
    Monitor,
    MonitorChannel,
    SentMessage,
    Session,
    TelegramAccount,
    TelegramCredential,
    TgAuthAttempt,
    User,
    UserIdentity,
)

logger = logging.getLogger(__name__)

# Таблицы тенанта в порядке удаления: сначала зависимые, потом их владельцы.
# `chat_avatars` и `worker_heartbeats` сюда НЕ входят — они общие: аватарка
# публичного канала одна на всех, отметка воркера не принадлежит никому.
TENANT_TABLES = (
    MonitorChannel,
    Monitor,
    SentMessage,
    FeedItem,
    LogEntry,
    Integration,
    Job,
    LLMUsage,
    LLMUsageSlice,
    LegacyImportRow,
    TgAuthAttempt,
    TelegramAccount,
    TelegramCredential,
    UserIdentity,
    Session,
)


async def export_account(db, user: User) -> dict:
    """Всё, что человек создал, — без единого доступа.

    Токен бота, ключ модели и строка сессии сюда не попадают намеренно: это
    не данные человека, а ключи. Выгрузка задумана как «забрать своё», и
    класть в файл, который уедет в почту или мессенджер, действующий доступ —
    значит раздать его вместе с файлом.
    """
    repo = TenantRepo(db, user.id)
    sources = list(await db.scalars(repo.query(Monitor).order_by(Monitor.id)))
    channels = list(
        await db.scalars(
            repo.query(MonitorChannel).order_by(
                MonitorChannel.monitor_id, MonitorChannel.position
            )
        )
    )
    by_source: dict[int, list[dict]] = {}
    for channel in channels:
        by_source.setdefault(channel.monitor_id, []).append(
            {
                "chat_target": channel.chat_target,
                "chat_title": channel.chat_title,
                "chat_username": channel.chat_username,
                "limit": channel.limit_count,
                "offset_hours": channel.offset_hours,
                "extract_prompt": channel.extract_prompt,
                "position": channel.position,
            }
        )

    feed = list(await db.scalars(repo.query(FeedItem).order_by(FeedItem.id.desc())))
    journal = list(await db.scalars(repo.query(LogEntry).order_by(LogEntry.id.desc())))

    return {
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "sources": [
            {
                # наружу публичный id, внутренние BIGINT не отдаются (9.10)
                "public_id": source.public_id,
                "title": source.title,
                "interval_minutes": source.interval_minutes,
                "is_active": source.is_active,
                "answer_prompt": source.answer_prompt,
                "stop_words": source.stop_words,
                "channels": by_source.get(source.id, []),
            }
            for source in sources
        ],
        "feed": [
            {
                "job_id": item.job_id,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "chat_title": item.chat_title,
                "chat_username": item.chat_username,
                "messages_count": item.messages_count,
                "ai_analysis": item.ai_analysis,
                "model_name": item.model_name,
                "delivery_status": item.delivery_status,
            }
            for item in feed
        ],
        "journal": [
            {
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                "event_type": entry.event_type,
                "status": entry.status,
                "chat_title": entry.chat_title,
                "details": entry.details,
            }
            for entry in journal
        ],
    }


async def delete_account(db, user: User, *, revoker=None) -> None:
    """Удалить тенанта целиком. Необратимо."""
    user_id = user.id

    if revoker is not None:
        try:
            await revoker(db, user_id)
        except Exception as exc:  # noqa: BLE001 — право уйти не ждёт чужой сервис
            # Журнал тенанта сейчас будет удалён — записывать некуда, кроме
            # лога процесса. Молчать нельзя: человек ушёл, а сессия осталась
            # в его списке устройств, и знать об этом должен оператор.
            logger.warning(
                "тенант %s удаляется, но сессия Telegram не завершена: %s",
                user_id,
                type(exc).__name__,
            )

    repo = TenantRepo(db, user_id)
    for model in TENANT_TABLES:
        await db.execute(repo.delete(model))
    # Сам пользователь — не тенантный ресурс: у строки `users` нет `user_id`,
    # и repo.delete() к ней неприменим по устройству.
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
