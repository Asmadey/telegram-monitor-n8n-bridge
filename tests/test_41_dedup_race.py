"""Задача 4.2 — атомарная дедупликация.

server.py:363 (filter_and_save_new_messages) делает SELECT, потом
INSERT. Между ними — окно: ручной запуск и тик планировщика по одному
каналу одновременно → оба считают пост новым → в n8n уходит ДВА
одинаковых вебхука. INSERT OR IGNORE спасает строку в базе, но
new_messages уже посчитан и отправлен.

Решение (план): дедупликация решается БАЗОЙ, одним запросом:
INSERT ... ON CONFLICT (user_id, chat_id, message_id) DO NOTHING
RETURNING message_id. Вернувшиеся id — и есть новые; не вернувшиеся —
уже были. Никакого SELECT перед INSERT.

Отступление от псевдокода теста в плане: там оба вызова получают ОДНУ
и ту же `db`. AsyncSession не допускает конкурентных вызовов (не
thread/coroutine-safe, гонка на greenlet). Гонка, которую проверяем, —
между ТРАНЗАКЦИЯМИ, а не корутинами одной сессии: тест открывает две
независимые сессии из движка (реальный сценарий: воркер + ручной
запуск — разные сессии).
"""

import asyncio
import datetime

import pytest
from sqlalchemy import text

from app.models import Monitor, SentMessage
from app.services.dedup import filter_new

CHAT_ID = -1001234567890


def _msgs(n: int = 50) -> list[dict]:
    # date — datetime, как отдаёт Telethon (колонка DateTime; ISO-строка
    # здесь падала StatementError'ом — ошибка теста, не реализации)
    return [
        {
            "id": i,
            "text": f"пост {i}",
            "date": datetime.datetime(2026, 9, 2, 0, 0, tzinfo=datetime.timezone.utc),
            "sender": "канал",
            "views": 100 + i,
        }
        for i in range(1, n + 1)
    ]


async def _seed_monitor(db, user_id: int) -> Monitor:
    monitor = Monitor(user_id=user_id, chat_target="@race", chat_id=CHAT_ID)
    db.add(monitor)
    await db.commit()
    return monitor


@pytest.mark.asyncio
async def test_concurrent_dedup_yields_each_message_once(db_engine, db, user_a):
    """Гонка из плана: два конкурентных filter_new по одному каналу —
    каждый пост считается новым РОВНО ОДИН раз."""
    monitor = await _seed_monitor(db, user_a.id)
    msgs = _msgs()

    # две НЕЗАВИСИМЫЕ сессии (см. докстринг модуля): AsyncSession
    # не позволяет корутинам делить её
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def run_one() -> list[dict]:
        async with sessionmaker() as session:
            return await filter_new(session, user_a.id, monitor.chat_id, msgs)

    a, b = await asyncio.gather(run_one(), run_one())

    ids = [m["id"] for m in a] + [m["id"] for m in b]
    assert len(ids) == len(set(ids)) == 50, "сообщение обработано дважды"


@pytest.mark.asyncio
async def test_filter_new_marks_and_returns_only_unseen(db, user_a):
    """Обычный путь: первый вызов — все новые (и записаны в БД), второй —
    пусто; возвращённые словари — исходные (данные для диспетчера)."""
    monitor = await _seed_monitor(db, user_a.id)
    msgs = _msgs(5)

    fresh = await filter_new(db, user_a.id, monitor.chat_id, msgs)
    assert [m["id"] for m in fresh] == [1, 2, 3, 4, 5], "первый вызов не вернул все"

    again = await filter_new(db, user_a.id, monitor.chat_id, msgs)
    assert again == [], "уже отправленные вернулись как новые"

    stored = (
        await db.execute(
            text("SELECT COUNT(*) FROM sent_messages WHERE user_id = :u"),
            {"u": user_a.id},
        )
    ).scalar()
    assert stored == 5, f"в sent_messages {stored} строк вместо 5"


@pytest.mark.asyncio
async def test_filter_new_scopes_by_user(db, user_a, user_b):
    """Дедупликация в разрезе ТЕНАНТА: один и тот же пост в одном канале —
    новый для каждого пользователя по отдельности (unique user_id+chat_id+
    message_id). Один забытый user_id — и B не получит пост, виденный A."""
    monitor = await _seed_monitor(db, user_a.id)
    msgs = _msgs(3)

    a1 = await filter_new(db, user_a.id, monitor.chat_id, msgs)
    b1 = await filter_new(db, user_b.id, monitor.chat_id, msgs)
    assert len(a1) == 3, "A не получил свои новые посты"
    assert len(b1) == 3, "дедуп пробросил строки A на B (нет user_id в ключе)"


@pytest.mark.asyncio
async def test_filter_new_dedupes_within_batch(db, user_a):
    """Один и тот же id ДВАЖДЫ в одном батче (дубль из Telegram) —
    вставка не падает от unique-нарушения, пост считается один раз."""
    monitor = await _seed_monitor(db, user_a.id)
    msgs = _msgs(3) + [{"id": 1, "text": "дубль первого"}]

    fresh = await filter_new(db, user_a.id, monitor.chat_id, msgs)
    ids = [m["id"] for m in fresh]
    assert ids.count(1) == 1, "дубль внутри батча прошёл как новый"
    assert len(ids) == 3, f"вернуто {len(ids)} постов вместо 3"


@pytest.mark.asyncio
async def test_filter_new_empty_batch_is_noop(db, user_a):
    """Пустой батч — нет запросов, нет строк (порт поведения оригинала)."""
    monitor = await _seed_monitor(db, user_a.id)
    assert await filter_new(db, user_a.id, monitor.chat_id, []) == []


def test_sent_messages_unique_constraint_declared():
    """Ключ дедупликации — (user_id, chat_id, message_id): без user_id
    мульти-тенантная дедупликация невозможна в принципе."""
    cols = set()
    for constraint in SentMessage.__table__.constraints:
        if type(constraint).__name__ == "UniqueConstraint":
            cols.update(col.name for col in constraint.columns)
    assert {"user_id", "chat_id", "message_id"} <= cols, (
        f"unique-ограничение не покрывает тенантный ключ: {cols}"
    )
