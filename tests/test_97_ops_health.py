"""Наблюдаемость: жива ли база, жив ли воркер, не стареет ли очередь (10.2).

До сих пор единственной проверкой был `GET /health` — «процесс жив».
Он отвечает 200 и когда воркер не поднят вовсе: web и worker это два
разных процесса, и здоровье первого ничего не говорит о втором.

А молчащий воркер — самый вероятный отказ этой архитектуры. Снаружи он
выглядит как «ничего не происходит»: лента пуста, задача висит в
`pending`, ошибок нет нигде. Ровно это и описано шестым пунктом чек-листа
`docs/DEPLOY.md`, и ровно так выглядел день 7 сентября, когда воркер
падал на отсутствующем ключе, а web отвечал 200.

Поэтому три числа, которых не хватало: доступна ли база, сколько секунд
назад воркер отметился, и сколько лет самой старой невыполненной задаче.

`/health` при этом НЕ меняется. Он публичный, и подробности состояния —
это подробности устройства: анониму знать, что у сервиса отстаёт очередь,
незачем (задача 4.7, C22).
"""

import datetime

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio

OPS = "/api/ops/health"


def _utc(**delta):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)


async def test_public_health_stays_a_bare_status(anon_client):
    """Публичная проверка не раскрывает состояния внутренностей."""
    response = await anon_client.get("/health")
    assert response.json() == {"status": "ok"}, (
        "в публичный /health протекли подробности состояния"
    )


async def test_ops_health_is_closed_to_anonymous(anon_client):
    assert (await anon_client.get(OPS)).status_code == 401


async def test_ops_health_reports_database_and_worker(anon_client, db, user):
    from app.services.ops import record_heartbeat

    await act_as(anon_client, db, user)
    await record_heartbeat(db, "worker", leader=True)
    await db.commit()

    body = (await anon_client.get(OPS)).json()
    assert body["database"]["ok"] is True
    assert body["worker"]["alive"] is True, "свежий удар сердца прочитан как мёртвый"
    assert body["worker"]["seconds_since_beat"] < 60
    assert body["worker"]["leader"] is True


async def test_silent_worker_is_reported_dead(anon_client, db, user):
    """Сердце, стучавшее час назад, — это остановившийся воркер."""
    from app.models import WorkerHeartbeat

    await act_as(anon_client, db, user)
    db.add(WorkerHeartbeat(name="worker", beat_at=_utc(hours=1), leader=True))
    await db.commit()

    worker = (await anon_client.get(OPS)).json()["worker"]
    assert worker["alive"] is False, (
        "молчащий воркер показан живым — это худший из возможных ответов: "
        "смотрящий решит, что дело не в нём"
    )
    assert worker["seconds_since_beat"] > 3000


async def test_worker_that_never_started_is_not_a_crash(anon_client, db, user):
    """Пустая таблица — «не запускался», а не 500."""
    await act_as(anon_client, db, user)
    worker = (await anon_client.get(OPS)).json()["worker"]
    assert worker["alive"] is False
    assert worker["seconds_since_beat"] is None
    assert worker.get("seen") is False


async def test_queue_age_is_visible(anon_client, db, user):
    """Возраст самой старой pending-задачи — то самое число, по которому
    видно, что воркер не разбирает очередь."""
    from app.models import Job

    await act_as(anon_client, db, user)
    db.add(Job(user_id=user.id, kind="poll_monitor", status="pending"))
    db.add(
        Job(
            user_id=user.id,
            kind="poll_monitor",
            status="pending",
            created_at=_utc(minutes=30),
        )
    )
    db.add(Job(user_id=user.id, kind="poll_monitor", status="done"))
    await db.commit()

    jobs = (await anon_client.get(OPS)).json()["jobs"]
    assert jobs["pending"] == 2, "выполненные задачи посчитаны как ожидающие"
    assert jobs["oldest_pending_seconds"] > 1500


async def test_ops_health_never_leaks_configuration(anon_client, db, user):
    """Ответ говорит о СОСТОЯНИИ, а не об устройстве: ни адреса базы, ни
    ключей, ни версий — это внутренняя диагностика, а не паспорт сервиса."""
    from app.services.ops import record_heartbeat

    await act_as(anon_client, db, user)
    await record_heartbeat(db, "worker", leader=True)
    await db.commit()

    raw = (await anon_client.get(OPS)).text.lower()
    for forbidden in ("postgres", "asyncpg", "://", "key", "token", "password"):
        assert forbidden not in raw, f"в ответе диагностики протекло: {forbidden}"


async def test_worker_beats_on_every_tick(db):
    """Сердце бьётся в тике, а не при старте: процесс, зависший внутри
    тика, обязан выглядеть мёртвым — иначе проверка бесполезна."""
    from sqlalchemy import select

    from app.models import WorkerHeartbeat
    from app.worker import Worker

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    worker = Worker(sessionmaker=_Maker())
    await worker._default_tick()
    await db.commit()

    beat = (await db.scalars(select(WorkerHeartbeat))).first()
    assert beat is not None, "тик прошёл, а воркер не отметился"
    assert beat.name == "worker"
