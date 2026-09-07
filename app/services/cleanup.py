"""Автоочистка базы (К2) — порт server.py:484.

Удаляет устаревшие журналы, сохранённые посты и карточки ленты. Отличий от
монолита два, и оба обязательны в мульти-тенанте.

1. Удаление ограничено одним пользователем. В оригинале это три `DELETE ...
   WHERE timestamp < ?` без user_id: запуск очистки одним клиентом стирал
   данные всех сразу.

2. Срок хранения — настройка тенанта, а не сервиса. Это выбор владельца
   данных, и он же обязательный элемент политики хранения для публичного
   сервиса.

Дедупликационные ключи сохраняются: очистка истории не делает прочитанный
пост новым. Незавершённый анализ и доставка остаются доступными для повтора.
"""

import datetime

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import TenantRepo, deleted_count
from app.models import FeedItem, Job, LogEntry, SentMessage


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def purge_older_than(
    db: AsyncSession, user_id: int, days: int, *, now: datetime.datetime | None = None
) -> dict[str, int]:
    """Удалить данные пользователя старше `days` дней; вернуть счётчики."""
    cutoff = (now or _utcnow()) - datetime.timedelta(days=days)
    removed: dict[str, int] = {}
    repo = TenantRepo(db, user_id)
    for name, model, column in (
        ("logs", LogEntry, LogEntry.timestamp),
        ("feed", FeedItem, FeedItem.created_at),
    ):
        stmt = delete(model).where(
            model.id.in_(repo.query(model).with_only_columns(model.id)), column < cutoff
        )
        if model is FeedItem:
            stmt = stmt.where(
                or_(
                    FeedItem.delivery_status.is_(None),
                    FeedItem.delivery_status == "SUCCESS",
                )
            )
        result = await db.execute(stmt)
        removed[name] = deleted_count(result)
    result = await db.execute(
        update(SentMessage)
        .where(
            SentMessage.id.in_(
                repo.query(SentMessage).with_only_columns(SentMessage.id)
            ),
            SentMessage.sent_at < cutoff,
            SentMessage.processed.is_(True),
            or_(SentMessage.text.is_not(None), SentMessage.sender.is_not(None)),
        )
        .values(text=None, sender=None, reactions_json="[]", post_url=None)
    )
    removed["messages"] = deleted_count(result)
    await db.execute(
        delete(Job).where(
            Job.id.in_(repo.query(Job).with_only_columns(Job.id)),
            Job.kind == "process_batch",
            Job.status == "done",
            Job.finished_at < cutoff,
        )
    )
    await db.commit()
    return removed
