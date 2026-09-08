"""Совпадение ключей шифрования видно фактом, а не догадкой (10.4).

7 сентября выяснение того, одинаков ли `APP_ENCRYPTION_KEY` у двух
сервисов, заняло несколько кругов переписки: значение секретное, сравнить
его глазами нельзя, а единственным симптомом был `InvalidToken` в журнале
раз в тридцать секунд.

Отпечаток решает это без раскрытия: первые 8 шестнадцатеричных знаков
SHA-256 от ключа. По ним нельзя восстановить ключ, но их достаточно, чтобы
увидеть — совпадают процессы или нет. Web считает свой при запросе, воркер
записывает свой вместе с ударом сердца.
"""

import hashlib

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio

OPS = "/api/ops/health"


async def test_fingerprint_identifies_the_key_without_revealing_it(_env):
    from app.config import get_settings
    from app.security.crypto import key_fingerprint

    key = get_settings().app_encryption_key
    assert key, "тест бессмыслен без ключа"

    mark = key_fingerprint()
    assert mark == hashlib.sha256(key.encode()).hexdigest()[:8]
    assert len(mark) == 8, "отпечаток должен быть коротким"
    assert key[:8] not in mark, "в отпечатке видна часть самого ключа"


async def test_ops_shows_both_sides_and_their_verdict(anon_client, db, user):
    """Один взгляд отвечает на вопрос «ключи совпадают?»."""
    from app.security.crypto import key_fingerprint
    from app.services.ops import record_heartbeat

    await act_as(anon_client, db, user)
    await record_heartbeat(db, "worker", leader=True, fingerprint=key_fingerprint())
    await db.commit()

    body = (await anon_client.get(OPS)).json()
    assert body["encryption"]["web"] == key_fingerprint()
    assert body["encryption"]["worker"] == key_fingerprint()
    assert body["encryption"]["match"] is True


async def test_mismatch_is_stated_outright(anon_client, db, user):
    """Разные ключи — не «возможно», а прямой ответ."""
    from app.services.ops import record_heartbeat

    await act_as(anon_client, db, user)
    await record_heartbeat(db, "worker", leader=True, fingerprint="deadbeef")
    await db.commit()

    encryption = (await anon_client.get(OPS)).json()["encryption"]
    assert encryption["worker"] == "deadbeef"
    assert encryption["match"] is False, (
        "разные отпечатки показаны как совпадение — проверка бесполезна"
    )


async def test_worker_that_never_beat_gives_no_verdict(anon_client, db, user):
    """Нечего сравнивать — и вердикта нет: «не совпадает» тут было бы ложью."""
    await act_as(anon_client, db, user)

    encryption = (await anon_client.get(OPS)).json()["encryption"]
    assert encryption["worker"] is None
    assert encryption["match"] is None


async def test_worker_writes_its_fingerprint_in_the_tick(db):
    from sqlalchemy import select

    from app.models import WorkerHeartbeat
    from app.security.crypto import key_fingerprint
    from app.worker import Worker

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    await Worker(sessionmaker=_Maker())._default_tick()
    await db.commit()

    beat = (await db.scalars(select(WorkerHeartbeat))).first()
    assert beat.key_fingerprint == key_fingerprint(), (
        "воркер не отмечает, каким ключом он работает — сравнить нечем"
    )
