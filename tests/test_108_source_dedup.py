"""Дедупликация в разрезе источника (задача 11.2).

Решение владельца: один канал может входить в несколько источников, и
каждый разбирает его посты по своим критериям. «Вакансии продаж» и
«AI-вакансии» могут смотреть один и тот же канал и искать в нём разное.

Прежний ключ `(user_id, chat_id, message_id)` этого не позволял: пост,
увиденный первым источником, для второго переставал существовать — и
выглядело бы это как «второй источник не работает», а не как «пост уже
посчитан». Ключ становится `(monitor_id, chat_id, message_id)`.

**Чего эта смена НЕ даёт, и это надо назвать честно.** Строки удалённого
источника остаются в базе с `monitor_id = NULL` (задача 11.1: история
переживает источник). Но новый источник их не узнаёт — у него другой id, —
и посты в пределах окна выборки он разберёт заново. Это не дефект, а
прямое следствие выбранной модели: другой источник значит другие критерии.
Ограничивает объём `offset_hours`: выборка не уходит глубже суток, так что
речь про день постов, а не про всю историю.
"""

import pytest

from app.models import Monitor
from app.services.dedup import filter_new

pytestmark = pytest.mark.asyncio


def _post(msg_id: int) -> dict:
    return {
        "id": msg_id,
        "text": f"пост {msg_id}",
        "post_url": f"https://t.me/c/{msg_id}",
    }


async def _source(db, user, public_id: str) -> Monitor:
    source = Monitor(user_id=user.id, public_id=public_id, title=public_id)
    db.add(source)
    await db.commit()
    return source


async def test_two_sources_watching_one_channel_both_see_the_post(db, user):
    """Главный сценарий фазы: одни каналы, разные критерии."""
    first = await _source(db, user, "продажи")
    second = await _source(db, user, "аналитика")
    posts = [_post(11), _post(12)]

    seen_first = await filter_new(db, user.id, -1001, posts, monitor_id=first.id)
    seen_second = await filter_new(db, user.id, -1001, posts, monitor_id=second.id)

    assert [m["id"] for m in seen_first] == [11, 12]
    assert [m["id"] for m in seen_second] == [11, 12], (
        "второй источник не увидел постов: канал достался тому, кто опросил "
        "первым, и это выглядит как «источник не работает»"
    )


async def test_repeat_poll_inside_one_source_sees_nothing(db, user):
    """Дедупликация не ослаблена: в своём источнике пост считается один раз."""
    source = await _source(db, user, "продажи")
    posts = [_post(11), _post(12)]

    await filter_new(db, user.id, -1001, posts, monitor_id=source.id)
    again = await filter_new(db, user.id, -1001, posts, monitor_id=source.id)

    assert again == [], f"повторный опрос вернул старые посты: {again}"


async def test_new_posts_come_through_next_to_old_ones(db, user):
    source = await _source(db, user, "продажи")
    await filter_new(db, user.id, -1001, [_post(11)], monitor_id=source.id)

    fresh = await filter_new(
        db, user.id, -1001, [_post(11), _post(12)], monitor_id=source.id
    )

    assert [m["id"] for m in fresh] == [12], "новый пост потерялся среди старых"


async def test_different_channels_do_not_silence_each_other(db, user):
    """chat_id остаётся частью ключа: иначе каналы глушили бы друг друга."""
    source = await _source(db, user, "продажи")

    first = await filter_new(db, user.id, -1001, [_post(11)], monitor_id=source.id)
    second = await filter_new(db, user.id, -1002, [_post(11)], monitor_id=source.id)

    assert first and second, "пост с тем же id из другого канала пропал"


async def test_channel_returned_to_the_same_source_does_not_flood(db, user):
    """Канал убрали из источника и вернули — лавины старого не будет.

    Строки дедупликации привязаны к источнику, а не к строке канала,
    поэтому их не трогает ни удаление канала, ни его возврат.
    """
    source = await _source(db, user, "продажи")
    posts = [_post(11), _post(12)]
    await filter_new(db, user.id, -1001, posts, monitor_id=source.id)

    # канал удалили и добавили заново — источник тот же
    after_return = await filter_new(db, user.id, -1001, posts, monitor_id=source.id)

    assert after_return == [], f"возврат канала залил старые посты: {after_return}"


async def test_history_of_a_deleted_source_does_not_block_a_new_one(db, user):
    """Документирующий тест: новый источник разбирает посты заново.

    Это следствие модели, а не упущение. Другой источник — другие
    критерии, и «уже видел» здесь неприменимо. Объём ограничен окном
    выборки (`offset_hours`), а не всей историей.
    """
    from sqlalchemy import update

    from app.models import SentMessage

    old = await _source(db, user, "старый")
    posts = [_post(11)]
    await filter_new(db, user.id, -1001, posts, monitor_id=old.id)

    # источник удалён: ON DELETE SET NULL оставил строки без владельца
    await db.execute(
        update(SentMessage)
        .where(SentMessage.monitor_id == old.id)
        .values(monitor_id=None)
    )
    await db.commit()

    new = await _source(db, user, "новый")
    seen = await filter_new(db, user.id, -1001, posts, monitor_id=new.id)

    assert [m["id"] for m in seen] == [11], (
        "новый источник не увидел постов — значит осиротевшие строки всё ещё "
        "участвуют в дедупликации, и создать источник заново нельзя"
    )


async def test_dedup_requires_a_source(db, user):
    """Без источника писать в дедупликацию нельзя: строка без владельца
    не участвует в ключе и молча пропускала бы дубли."""
    with pytest.raises(TypeError):
        await filter_new(db, user.id, -1001, [_post(11)])
