"""Доработки крестика журнала по локальному ревью PR #109 (задача 13.29).

1. След очистки нельзя стереть крестиком. Очистка журнала пишет строку
   `SYSTEM` «Журнал событий очищен пользователем» — действие, стирающее
   историю, обязано оставлять след. Крестик на этой строке стирал след без
   следа, и правило очистки теряло смысл.
2. Номер вне BIGINT — 404, а не 500: Postgres отвечал ошибкой переполнения.
3. «Загрузить ещё» — по курсору (`before_id`), а не по сдвигу. Удаление строки,
   пришедшее во время дозагрузки, сдвигало страницы, и одна запись не
   показывалась никогда.
4. Удаление последней загруженной строки не показывает «записей нет», пока на
   сервере они есть; таблица не перерисовывается целиком ради одной строки.
"""

from pathlib import Path

import pytest
from conftest import act_as
from sqlalchemy import select

from app.models import LogEntry

pytestmark = pytest.mark.asyncio

JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "logs.js").read_text(
    encoding="utf-8"
)


async def _entries(db, user_id: int, count: int) -> list[int]:
    ids = []
    for index in range(count):
        entry = LogEntry(
            user_id=user_id,
            event_type="SCHEDULER_POLL",
            status="INFO",
            details=f"запись {index}",
        )
        db.add(entry)
        await db.commit()
        ids.append(entry.id)
    return ids


async def test_the_trace_of_a_clear_cannot_be_crossed_out(anon_client, db, user):
    await act_as(anon_client, db, user)
    await anon_client.delete("/api/logs")
    trace = await db.scalar(
        select(LogEntry.id).where(
            LogEntry.user_id == user.id, LogEntry.event_type == "SYSTEM"
        )
    )
    assert trace, "очистка не оставила следа — тест не о чем"

    resp = await anon_client.delete(f"/api/logs/{trace}")

    assert resp.status_code == 409, (
        f"след очистки удалён крестиком ({resp.status_code}): действие, "
        "стирающее историю, осталось без следа"
    )
    assert await db.get(LogEntry, trace) is not None


async def test_an_id_out_of_range_is_not_found(anon_client, db, user):
    await act_as(anon_client, db, user)
    resp = await anon_client.delete(f"/api/logs/{2**63}")
    assert resp.status_code == 404, f"номер вне BIGINT → {resp.status_code}"


async def test_load_more_follows_a_cursor_not_an_offset(anon_client, db, user):
    """Удаление между страницами не прячет запись со следующей страницы."""
    first, second, third, fourth, fifth = await _entries(db, user.id, 5)
    await act_as(anon_client, db, user)

    page = (await anon_client.get("/api/logs?limit=2")).json()["logs"]
    assert [e["id"] for e in page] == [fifth, fourth]
    await anon_client.delete(f"/api/logs/{fourth}")

    more = (await anon_client.get(f"/api/logs?limit=2&before_id={fourth}")).json()
    assert [e["id"] for e in more["logs"]] == [third, second], (
        f"дозагрузка после удаления потеряла запись: {more['logs']}"
    )


def test_the_interface_asks_for_the_next_page_by_cursor():
    assert "before_id" in JS, "«Загрузить ещё» всё ещё листает сдвигом"


def test_the_interface_offers_no_cross_on_the_trace_of_a_clear():
    assert "'SYSTEM'" in JS, "крестик рисуется и на следе очистки журнала"


def test_one_deleted_row_does_not_redraw_the_table():
    # Обработчик — до `});` в начале строки: внутри него `{ method: 'DELETE' });`
    # тоже кончается на `});`, и первая версия теста резала по нему.
    handler = JS.split('data-action="delete-log"]\')', 1)[-1].split("\n});", 1)[0]
    assert "closest('tr')" in handler, "строка не убирается сама по себе"
    assert "renderLogs()" not in handler.split("if (!currentLogs.length)")[0], (
        "ради одной строки перерисовывается вся таблица"
    )
    assert "loadLogs()" in handler, (
        "после удаления последней загруженной строки таблица скажет «записей "
        "нет», хотя на сервере они есть"
    )
