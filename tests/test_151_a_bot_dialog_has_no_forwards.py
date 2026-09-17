"""Пост из бота ронял весь канал (задача 13.13).

Найдено владельцем: источник «ТОП-вакансии» прислал тревогу «не дал ни одной
находки. getmatch: бот с IT-вакансиями — причина в журнале». В журнале прода:

    POLL_ERROR | getmatch: бот с IT-вакансиями
    Канал «getmatch: бот с IT-вакансиями» не сохранён: IntegrityError:
    NotNullViolationError: null value in column "forwards" of relation
    "sent_messages" violates not-null constraint

**Причина — `dict.get` с умолчанием там, где значение приходит `None`.**
`messages.py` кладёт в словарь `"forwards": getattr(msg, "forwards", None)`.
У поста БРОАДКАСТ-канала это число. У поста из диалога с ботом
(`t.me/g_jobbot/…`) счётчика пересылок не существует вовсе, и Telethon
отдаёт ключ со значением `None` — атрибут есть, значения нет. Дальше
`_to_row` пишет `msg.get("forwards", 0)`, а умолчание у `get` срабатывает
только когда КЛЮЧА НЕТ. Ключ был. В колонку `NOT NULL` уходил `None`.

**Цена — весь канал за прогон, а не один пост.** Посты вставляются одним
многострочным `INSERT`, поэтому одна плохая строка отменяет вставку целиком:
канал помечается неразобранным, его посты не сохраняются, и при следующем
прогоне всё повторяется — в журнале два одинаковых отказа на одном и том же
сообщении с разницей в шесть часов.

Тревога 13.3 при этом сработала верно: в том прогоне у остальных каналов
новых постов не было, а единственный канал с постами не сохранился —
«ни одной находки» было правдой.

Защита ставится шире одного поля: любая колонка `NOT NULL`, которую
заполняет `_to_row`, не должна получать `None` ни при каком входе.
"""

import pytest

from app.models import Monitor, SentMessage
from app.services.dedup import _to_row, filter_new

# Пост из диалога с ботом ровно в той форме, в какой его отдаёт `messages.py`:
# ключи на месте, счётчиков нет.
BOT_POST = {
    "id": 709539,
    "date": "2026-09-16T13:44:13+00:00",
    "sender": "getmatch",
    "text": "Product manager [Remote]",
    "has_media": False,
    "views": None,
    "forwards": None,
    "reactions_count": 0,
    "reactions": [],
    "post_url": "https://t.me/g_jobbot/709539",
}


def _row(**overrides) -> dict:
    post = {**BOT_POST, **overrides}
    return _to_row(1, 2, -1003, post)


def test_a_post_without_forwards_still_gets_a_number():
    """Главное утверждение: `None` не доезжает до колонки `NOT NULL`."""
    assert _row()["forwards"] == 0, (
        "в колонку NOT NULL уходит None — весь канал не сохранится"
    )


def test_a_real_count_is_not_flattened():
    """Антивакуум: обнулить всем подряд — тоже «зелёный»."""
    assert _row(forwards=17)["forwards"] == 17, "настоящий счётчик потерян"


def test_no_not_null_column_ever_receives_none():
    """Защита шире одного поля: правило, а не заплатка на `forwards`.

    Список колонок берётся из САМОЙ модели, поэтому новая колонка `NOT NULL`
    попадёт под проверку сама, без правки теста.
    """
    empty = dict.fromkeys(BOT_POST, None)
    empty["id"] = 1  # без id строка не строится вовсе
    row = _to_row(1, 2, -1003, empty)

    required = {
        column.name
        for column in SentMessage.__table__.columns
        if not column.nullable and not column.primary_key
    }
    offenders = sorted(name for name in required & row.keys() if row[name] is None)
    assert not offenders, (
        f"колонки NOT NULL получили None и уронят вставку всего канала: {offenders}"
    )


@pytest.mark.asyncio
async def test_the_whole_channel_survives_one_bot_post(db, user):
    """Поведение: один пост без счётчика не отменяет вставку остальных."""
    source = Monitor(user_id=user.id, public_id="src-bot", title="ТОП-вакансии")
    db.add(source)
    await db.commit()

    posts = [
        BOT_POST,
        {**BOT_POST, "id": 709540, "text": "Ещё вакансия"},
    ]
    fresh = await filter_new(db, user.id, -1003, posts, monitor_id=source.id)

    assert len(fresh) == 2, f"канал потерян целиком из-за одного поста: {fresh}"
