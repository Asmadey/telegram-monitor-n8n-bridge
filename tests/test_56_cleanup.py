"""К2 — автоочистка базы (server.py:484, 1832-1860).

Два отличия от монолита, оба обязательны в мульти-тенанте: удаление
ограничено одним пользователем (в оригинале три `DELETE ... WHERE
timestamp < ?` без user_id — запуск одним клиентом стирал данные всех),
и срок хранения принадлежит тенанту, а не сервису.
"""

import datetime

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import LogEntry
from app.services.cleanup import purge_older_than

pytestmark = pytest.mark.asyncio


def _old(user_id: int, days: int) -> LogEntry:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return LogEntry(
        user_id=user_id, event_type="X", status="INFO", details="старое", timestamp=ts
    )


async def _logs_of(db, user_id: int) -> int:
    return await db.scalar(
        select(func.count()).select_from(LogEntry).where(LogEntry.user_id == user_id)
    )


async def test_purge_touches_only_the_given_user(db, user_a, user_b):
    """Ключевая проверка: очистка одного тенанта не трогает другого."""
    db.add(_old(user_a.id, 90))
    db.add(_old(user_b.id, 90))
    await db.commit()

    removed = await purge_older_than(db, user_a.id, days=30)
    assert removed["logs"] == 1
    assert await _logs_of(db, user_b.id) == 1, "очистка задела другого пользователя"


async def test_recent_rows_survive(db, user):
    db.add(_old(user.id, 1))
    db.add(_old(user.id, 90))
    await db.commit()

    await purge_older_than(db, user.id, days=30)
    assert await _logs_of(db, user.id) == 1, "удалены свежие записи"


async def test_config_roundtrip_and_run(anon_client, db, user):
    await act_as(anon_client, db, user)

    saved = await anon_client.post("/api/cleanup", json={"enabled": True, "days": 14})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"enabled": True, "days": 14, "last_run": None}

    db.add(_old(user.id, 60))
    await db.commit()

    run = await anon_client.post("/api/cleanup/run-now")
    assert run.status_code == 200, run.text
    assert run.json()["removed"]["logs"] == 1
    assert (await anon_client.get("/api/cleanup")).json()["last_run"] is not None


async def test_absurd_retention_is_rejected(anon_client, db, user):
    """Срок в один день молча стёр бы дедупликацию: список закрыт намеренно."""
    await act_as(anon_client, db, user)
    resp = await anon_client.post("/api/cleanup", json={"enabled": True, "days": 1})
    assert resp.status_code == 400, resp.text
