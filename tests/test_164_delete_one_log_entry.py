"""Удалить одну запись журнала — крестик в конце строки (задача 13.28).

Заказ владельца 2026-10-15, напомнен 2026-10-16 снимком вкладки «Журнал»:
журнал можно было только очистить целиком (`DELETE /api/logs`). Строка, которая
мешает, уходила лишь вместе со всей историей.

Три решения, которые держит тест.

1. Чужая запись — 404, а не 403: 403 подтвердил бы, что такая запись есть.
2. Удаление одной строки НЕ пишет в журнал «удалена запись». Человек убирает
   строку, чтобы её не было; новая строка на её месте свела бы крестик на нет.
   След остаётся в журнале процесса — там, где его видит оператор.
3. Отметки «прочитано» (`sent_messages`) — другая таблица. Удалённая запись
   журнала не делает пост снова новым, и об этом тест говорит явно: иначе
   крестик выглядел бы способом «разобрать заново».
"""

import logging
from pathlib import Path

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import LogEntry, SentMessage

pytestmark = pytest.mark.asyncio

STATIC = Path(__file__).resolve().parent.parent / "static"


async def _entry(db, user_id: int, details: str) -> int:
    entry = LogEntry(
        user_id=user_id,
        event_type="SCHEDULER_POLL",
        status="SKIPPED_DEDUP",
        details=details,
    )
    db.add(entry)
    await db.commit()
    return entry.id


async def _left(db, user_id: int) -> list[str]:
    rows = await db.scalars(
        select(LogEntry.details)
        .where(LogEntry.user_id == user_id)
        .order_by(LogEntry.id)
    )
    return list(rows)


async def test_the_cross_removes_exactly_that_row(anon_client, db, user):
    keep = await _entry(db, user.id, "оставить")
    drop = await _entry(db, user.id, "удалить")
    assert keep != drop
    await act_as(anon_client, db, user)

    resp = await anon_client.delete(f"/api/logs/{drop}")

    assert resp.status_code == 200, resp.text
    assert await _left(db, user.id) == ["оставить"], (
        "удалена не та строка, или на месте удалённой появилась новая"
    )


async def test_another_cabinets_row_is_not_found(anon_client, db, user_a, user_b):
    foreign = await _entry(db, user_a.id, "чужая")
    await act_as(anon_client, db, user_b)

    resp = await anon_client.delete(f"/api/logs/{foreign}")

    assert resp.status_code == 404, (
        f"на чужую запись ответ {resp.status_code}: 403 подтвердил бы, что она есть"
    )
    assert await _left(db, user_a.id) == ["чужая"], "чужая запись удалена"


async def test_a_missing_row_is_not_found(anon_client, db, user):
    await act_as(anon_client, db, user)
    resp = await anon_client.delete("/api/logs/987654")
    assert resp.status_code == 404, resp.text


async def test_the_trace_goes_to_the_process_log(anon_client, db, user, caplog):
    drop = await _entry(db, user.id, "удалить")
    await act_as(anon_client, db, user)

    with caplog.at_level(logging.INFO):
        await anon_client.delete(f"/api/logs/{drop}")

    # Только свой логгер: строка httpx «DELETE /api/logs/N» несёт тот же номер
    # и зеленила бы тест без всякого следа.
    assert any(
        r.name == "app.api.journal" and str(drop) in r.getMessage()
        for r in caplog.records
    ), "удаление не оставило следа даже в журнале процесса"


async def test_the_read_marks_are_not_touched(anon_client, db, user):
    db.add(SentMessage(user_id=user.id, chat_id=-100500, message_id=37142))
    await db.commit()
    drop = await _entry(db, user.id, "Опрос «БАЛИ» завершён: новых постов нет.")
    await act_as(anon_client, db, user)

    await anon_client.delete(f"/api/logs/{drop}")

    marks = await db.scalar(
        select(func.count())
        .select_from(SentMessage)
        .where(SentMessage.user_id == user.id)
    )
    assert marks == 1, "удаление записи журнала стёрло отметку «прочитано»"


def test_every_row_of_the_journal_has_a_cross():
    js = (STATIC / "js" / "logs.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-action="delete-log"' in js, "в строке журнала нет крестика"
    assert "/api/logs/${" in js, "крестик не обращается к записи по её id"
    assert 'colspan="5"' not in js and 'colspan="5"' not in html, (
        "у таблицы журнала шестая колонка, а пустая строка всё ещё на пять"
    )
