"""Атомарная дедупликация (задача 4.2 PLAN.md).

Оригинал (server.py:363) делал SELECT, потом INSERT: между ними окно
гонки — ручной запуск и тик планировщика по одному каналу одновременно
считают один пост новым ОБАЖДЫ → в n8n уходят два одинаковых вебхука.
INSERT OR IGNORE спасал только строку в базе, new_messages уже был
посчитан и отправлен.

Здесь дедупликацию решает БАЗОЙ, одним запросом:

    INSERT INTO sent_messages (...) VALUES ...
    ON CONFLICT (user_id, chat_id, message_id) DO NOTHING
    RETURNING message_id

Вернувшиеся id — и есть новые; не вернувшиеся — уже были (транзакция
ставит unique-замок, конкурентная вставка в это же время просто
проглотится). Никакого SELECT перед INSERT.
"""

import datetime
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SentMessage


def _to_row(user_id: int, chat_id: int, msg: dict) -> dict:
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": msg["id"],
        "date": msg.get("date"),
        "sender": msg.get("sender"),
        "text": (msg.get("text") or "")[:1000],
        "views": msg.get("views"),
        "forwards": msg.get("forwards", 0),
        "has_media": bool(msg.get("has_media")),
        "reactions_count": msg.get("reactions_count", 0),
        "reactions_json": json.dumps(msg.get("reactions", []), ensure_ascii=False),
        "post_url": msg.get("post_url"),
        "sent_at": datetime.datetime.now(datetime.timezone.utc),
    }


def _insert_for(dialect_name: str) -> Callable[..., Any]:
    """Диалектный insert: оба поддерживают ON CONFLICT ... DO NOTHING.

    Выбирается МОДУЛЬ, а не одно и то же имя (условный ре-импорт insert
    даёт mypy «incompatible import»: типы sqlite.dml.Insert и
    postgresql.dml.Insert — разные классы). Возвращаемый тип общий
    Callable: stmt строит ровно один вызов."""
    module = sqlite if dialect_name == "sqlite" else postgresql
    return module.insert
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    return insert


async def filter_new(
    db: AsyncSession, user_id: int, chat_id: int, messages: list[dict]
) -> list[dict]:
    """Вернуть (и пометить) только НОВЫЕ сообщения — исходные словари в
    порядке входа, данные для диспетчера Фазы 5."""
    if not messages:
        return []

    rows = [_to_row(user_id, chat_id, m) for m in messages if m.get("id")]
    if not rows:
        return []

    insert = _insert_for(db.bind.dialect.name)
    stmt = (
        insert(SentMessage)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "chat_id", "message_id"])
        .returning(SentMessage.message_id)
    )
    result = await db.execute(stmt)
    inserted_ids = set(result.scalars())
    await db.commit()

    # исходный порядок входа; дубль внутри батча встречается один раз
    fresh: list[dict] = []
    seen: set[int] = set()
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in inserted_ids and msg_id not in seen:
            seen.add(msg_id)
            fresh.append(msg)
    return fresh
