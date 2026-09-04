"""Автоочистка базы (К2) — порт server.py:484.

Удаляет устаревшие журналы, сохранённые посты и карточки ленты. Отличий от
монолита два, и оба обязательны в мульти-тенанте.

1. Удаление ограничено одним пользователем. В оригинале это три `DELETE ...
   WHERE timestamp < ?` без user_id: запуск очистки одним клиентом стирал
   данные всех сразу.

2. Срок хранения — настройка тенанта, а не сервиса. Это выбор владельца
   данных, и он же обязательный элемент политики хранения для публичного
   сервиса.

Дедупликация: `sent_messages` чистится вместе с прочим, и это осознанно —
пост, удалённый из истории, снова считается новым. Поэтому срок хранения
имеет смысл держать заметно больше интервала опроса.
"""

import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import deleted_count
from app.models import FeedItem, LogEntry, SentMessage


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def purge_older_than(
    db: AsyncSession, user_id: int, days: int, *, now: datetime.datetime | None = None
) -> dict[str, int]:
    """Удалить данные пользователя старше `days` дней; вернуть счётчики."""
    cutoff = (now or _utcnow()) - datetime.timedelta(days=days)
    removed: dict[str, int] = {}
    for name, model, column in (
        ("logs", LogEntry, LogEntry.timestamp),
        ("messages", SentMessage, SentMessage.sent_at),
        ("feed", FeedItem, FeedItem.created_at),
    ):
        result = await db.execute(
            delete(model).where(model.user_id == user_id, column < cutoff)
        )
        removed[name] = deleted_count(result)
    await db.commit()
    return removed
