"""Исход последнего прогона — на карточке источника, и по расписанию тоже (13.32).

13.26 научила кнопку «Запустить» называть исход своего ручного прогона. Прогон
по расписанию задачи не заводит, и его исход оставался только в журнале:
источник, который неделю отвечает «новых постов нет» или «каналы не
прочитаны», на карточке выглядел так же, как работающий.

Исход пишется в источник (`monitors.last_outcome`, ревизия 0021, только
добавление) после каждого опроса — и по расписанию, и вручную, — и карточка
говорит его словами рядом со временем последнего прогона.
"""

import re
from pathlib import Path

import pytest
from conftest import act_as
from test_58_worker_body import _worker
from test_112_map_reduce import ChannelTelegram, _post, _source

from app.models import Monitor

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_a_scheduled_run_leaves_its_outcome_on_the_source(db, user):
    source = await _source(db, user, channels=[("@alpha", -1001, 5, "искать")])
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [])}))

    await worker.run_schedule(db)

    stored = await db.get(Monitor, source.id, populate_existing=True)
    assert getattr(stored, "last_outcome", None) == "no_new", (
        "исход прогона по расписанию остался только в журнале"
    )


@pytest.mark.asyncio
async def test_a_manual_run_leaves_its_outcome_too(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@alpha", -1001, 5, "искать")])
    await anon_client.post(f"/api/sources/{source.public_id}/run")
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [_post(11)])}))

    await worker.run_jobs(db)

    stored = await db.get(Monitor, source.id, populate_existing=True)
    assert getattr(stored, "last_outcome", None) == "dispatched", (
        "ручной прогон не оставил исхода на источнике"
    )


@pytest.mark.asyncio
async def test_the_card_carries_the_outcome(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@alpha", -1001, 5, "искать")])
    worker = _worker(db, telegram=ChannelTelegram({-1001: ("@alpha", [])}))
    await worker.run_schedule(db)

    cards = (await anon_client.get("/api/sources")).json()["sources"]
    card = next(c for c in cards if c["public_id"] == source.public_id)
    assert card.get("last_outcome") == "no_new", f"карточка не знает исхода: {card}"


def test_the_revision_only_adds_the_column():
    revisions = list((ROOT / "alembic" / "versions").glob("0021_*.py"))
    assert revisions, "нет ревизии 0021 с колонкой исхода"
    body = revisions[0].read_text(encoding="utf-8")
    upgrade = body[body.find("def upgrade") : body.find("def downgrade")]
    assert "add_column" in upgrade and "last_outcome" in upgrade
    assert not re.search(r"drop_column|alter_column", upgrade), (
        "ревизия не только добавляет: миграция идёт до старта нового кода (факт 2)"
    )


def test_the_card_says_the_outcome_in_words():
    js = (ROOT / "static" / "js" / "sources.js").read_text(encoding="utf-8")
    assert "s.last_outcome" in js, "карточка источника не показывает исход"
