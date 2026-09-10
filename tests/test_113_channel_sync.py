"""Добавленный канал попадает в источник, а не мимо него (11.6а).

Конвейер 11.4 опрашивает источник через строки `monitor_channels`.
Существующий эндпоинт `/api/monitors` при этом продолжал заполнять только
старые колонки самого источника — то есть канал, добавленный из
интерфейса ПОСЛЕ выкладки конвейера, не опрашивался бы никогда.

Снаружи это худший вид отказа: канал добавлен, виден в списке, ошибок нет,
а постов из него не приходит. Ни одного сигнала.

Разрыв возник между двумя зелёными задачами: 11.4 перевела чтение на новую
таблицу, а запись осталась на старой. Обе стороны по отдельности верны —
не стыкуется шов. Тот же класс, что 9.10 (интерфейс адресовал канал полем,
которого не было в ответе) и 9.13 (сервер различал три состояния секрета,
интерфейс — два).
"""

import pytest
from conftest import act_as
from sqlalchemy import select

from app.models import MonitorChannel

pytestmark = pytest.mark.asyncio


class _Entity:
    id = -1001234567890
    title = "Тестовый канал"
    username = "example"


@pytest.fixture
def resolver(app):
    from app.api.monitors import get_entity_resolver

    async def _entity(target):
        return _Entity()

    app.dependency_overrides[get_entity_resolver] = lambda: _entity
    yield
    app.dependency_overrides.pop(get_entity_resolver, None)


async def _create(anon_client, **body):
    payload = {"chat_target": "@example", "interval_minutes": 60}
    payload.update(body)
    return await anon_client.post("/api/monitors", json=payload)


async def test_added_channel_becomes_a_row_the_pipeline_can_see(
    anon_client, db, user, resolver
):
    await act_as(anon_client, db, user)

    created = await _create(anon_client, limit=50, prompt="искать продажи")
    assert created.status_code in (200, 201), created.text

    channels = list(await db.scalars(select(MonitorChannel)))
    assert len(channels) == 1, (
        "источник создан без канала — конвейер не найдёт, что опрашивать, "
        "и канал будет виден в списке, но молчать без единой ошибки"
    )
    channel = channels[0]
    assert channel.chat_target == "@example"
    assert channel.chat_id == _Entity.id
    assert channel.limit_count == 50, "лимит не доехал до канала"
    assert channel.extract_prompt == "искать продажи", "промпт не доехал до канала"


async def test_editing_the_source_updates_its_channel(anon_client, db, user, resolver):
    await act_as(anon_client, db, user)
    created = (await _create(anon_client)).json()

    await anon_client.patch(
        f"/api/monitors/{created['public_id']}",
        json={"limit": 5, "prompt": "новые критерии"},
    )

    channel = (await db.scalars(select(MonitorChannel))).first()
    await db.refresh(channel)
    assert channel.limit_count == 5, "правка лимита не дошла до канала"
    assert channel.extract_prompt == "новые критерии", "правка промпта не дошла"


async def test_deactivating_the_source_deactivates_its_channel(
    anon_client, db, user, resolver
):
    await act_as(anon_client, db, user)
    created = (await _create(anon_client)).json()

    await anon_client.patch(
        f"/api/monitors/{created['public_id']}", json={"is_active": False}
    )

    channel = (await db.scalars(select(MonitorChannel))).first()
    await db.refresh(channel)
    assert channel.is_active is False


async def test_added_channel_is_polled_by_the_pipeline(anon_client, db, user, resolver):
    """Сквозная проверка: добавленный канал реально доходит до выборки."""
    from test_58_worker_body import _worker
    from test_112_map_reduce import ChannelTelegram

    from app.models import Monitor

    await act_as(anon_client, db, user)
    await _create(anon_client)

    source = (await db.scalars(select(Monitor))).first()
    telegram = ChannelTelegram({_Entity.id: ("@example", [{"id": 11, "text": "пост"}])})
    worker = _worker(db, telegram=telegram)
    worker.llm = lambda *a, **k: _noop()

    await worker.poll_source(db, source)

    assert telegram.limits, "конвейер не дошёл до выборки по добавленному каналу"


async def _noop():
    return "находка"
