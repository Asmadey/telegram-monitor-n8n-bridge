"""Забыть одну отметку «прочитано» — крестик во вкладке «Сообщения» (задача 13.31).

Отметка (`sent_messages`) — то, по чему прогон узнаёт уже разобранный пост.
Сбросить можно было только все отметки источника разом. Крестик забывает одну:
если пост ещё в окне канала, следующий прогон разберёт его как новый.

Чужая отметка — 404, номер вне BIGINT — 404, как у записи журнала (13.29).
"""

from pathlib import Path

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import SentMessage

pytestmark = pytest.mark.asyncio

STATIC = Path(__file__).resolve().parent.parent / "static"


async def _mark(db, user_id: int, message_id: int) -> int:
    row = SentMessage(user_id=user_id, chat_id=-100500, message_id=message_id)
    db.add(row)
    await db.commit()
    return row.id


async def _marks(db, user_id: int) -> list[int]:
    rows = await db.scalars(
        select(SentMessage.message_id)
        .where(SentMessage.user_id == user_id)
        .order_by(SentMessage.id)
    )
    return list(rows)


async def test_the_cross_forgets_exactly_that_mark(anon_client, db, user):
    await _mark(db, user.id, 11)
    drop = await _mark(db, user.id, 12)
    await act_as(anon_client, db, user)

    resp = await anon_client.delete(f"/api/messages/{drop}")

    assert resp.status_code == 200, resp.text
    assert await _marks(db, user.id) == [11], "забыта не та отметка"


async def test_a_forgotten_post_is_new_again(anon_client, db, user):
    """Ради этого крестик и существует: пост снова проходит дедупликацию.

    Отметка — с источником: ключ дедупликации частичный, строки без источника
    в него не входят, и без источника тест зеленел бы и без удаления.
    """
    from test_112_map_reduce import _source

    from app.services.dedup import filter_new

    source = await _source(db, user, channels=[])
    post = [{"id": 12, "text": "пост"}]
    await filter_new(db, user.id, -100500, post, monitor_id=source.id)
    again = await filter_new(db, user.id, -100500, post, monitor_id=source.id)
    assert again == [], "антивакуум: до удаления пост обязан считаться прочитанным"
    drop = await db.scalar(select(SentMessage.id).where(SentMessage.message_id == 12))
    await act_as(anon_client, db, user)

    await anon_client.delete(f"/api/messages/{drop}")

    fresh = await filter_new(db, user.id, -100500, post, monitor_id=source.id)
    assert [m["id"] for m in fresh] == [12], (
        "забытый пост всё ещё считается прочитанным"
    )


async def test_another_cabinets_mark_is_not_found(anon_client, db, user_a, user_b):
    foreign = await _mark(db, user_a.id, 11)
    await act_as(anon_client, db, user_b)

    resp = await anon_client.delete(f"/api/messages/{foreign}")

    assert resp.status_code == 404, f"чужая отметка → {resp.status_code}"
    total = await db.scalar(select(func.count()).select_from(SentMessage))
    assert total == 1, "чужая отметка удалена"


async def test_an_id_out_of_range_is_not_found(anon_client, db, user):
    await act_as(anon_client, db, user)
    resp = await anon_client.delete(f"/api/messages/{2**63}")
    assert resp.status_code == 404, f"номер вне BIGINT → {resp.status_code}"


def test_every_row_of_the_messages_tab_has_a_cross():
    js = (STATIC / "js" / "messages.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-action="forget-mark"' in js, "в строке «Сообщений» нет крестика"
    assert "/api/messages/${" in js, "крестик не обращается к отметке по её id"
    assert (
        'colspan="6"' not in js
        and 'id="messagesTableBody">\n              <tr>\n                <td colspan="6"'
        not in html
    ), "у таблицы «Сообщений» седьмая колонка, а пустая строка всё ещё на шесть"
