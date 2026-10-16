"""Исход прогона виден в интерфейсе, а не только в журнале (задача 13.26).

Найдено владельцем 2026-10-15 на источнике «БАЛИ»: «Запустить» нажато пять раз
подряд, каждый раз «поставлен в очередь», и ни одного сообщения в боте. В
журнале при этом пять строк «новых постов нет»: каждый пост разбирается один
раз за жизнь источника, и повторный прогон честно молчал. Но кнопка обещала
«результат появится в ленте» — и человек решил, что сломался запуск.

Исход опроса (`no_new`, `filtered_out`, `failed`, `dispatched`) теперь хранится
в задаче, `/status` отдаёт его вместе с номером задачи, а кнопка говорит его
словами, когда прогон закончился.
"""

from pathlib import Path

import pytest
from conftest import act_as
from test_58_worker_body import _worker
from test_112_map_reduce import ChannelTelegram, _post, _source

pytestmark = pytest.mark.asyncio

STATIC = Path(__file__).resolve().parent.parent / "static"


async def _run_once(anon_client, db, user, posts) -> tuple[str, dict]:
    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@alpha", -1001, 5, "искать")])
    queued = (await anon_client.post(f"/api/sources/{source.public_id}/run")).json()
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", posts)}))
    await worker.run_jobs(db)
    status = (await anon_client.get(f"/api/sources/{source.public_id}/status")).json()
    return queued["job_id"], status


async def test_a_run_without_new_posts_says_so(anon_client, db, user):
    job_id, status = await _run_once(anon_client, db, user, posts=[])
    last = status.get("last_run") or {}
    assert last.get("job_id") == job_id, (
        f"состояние источника не знает, чем кончился прогон {job_id}: {status}"
    )
    assert last.get("outcome") == "no_new", (
        f"«новых постов нет» осталось только в журнале: {status}"
    )


async def test_a_run_with_new_posts_says_it_was_delivered(anon_client, db, user):
    job_id, status = await _run_once(anon_client, db, user, posts=[_post(11)])
    last = status.get("last_run") or {}
    assert (last.get("job_id"), last.get("outcome")) == (job_id, "dispatched"), (
        f"прогон с новыми постами не отличим от пустого: {status}"
    )


async def test_a_running_source_has_no_stale_outcome_for_the_new_run(
    anon_client, db, user
):
    """Пока прогон идёт, его исхода нет: старый исход не выдаётся за новый."""
    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@alpha", -1001, 5, "искать")])
    queued = (await anon_client.post(f"/api/sources/{source.public_id}/run")).json()
    status = (await anon_client.get(f"/api/sources/{source.public_id}/status")).json()
    assert (status.get("last_run") or {}).get("job_id") != queued["job_id"], (
        f"исход выдан до того, как прогон прошёл: {status}"
    )


def test_the_run_button_tells_the_outcome_in_words():
    js = (STATIC / "js" / "sources.js").read_text(encoding="utf-8")
    assert "last_run" in js, "кнопка «Запустить» не читает исход прогона"
    assert "Новых постов нет" in js, (
        "исход «новых постов нет» не назван словами — человек снова решит, "
        "что сломался запуск"
    )
