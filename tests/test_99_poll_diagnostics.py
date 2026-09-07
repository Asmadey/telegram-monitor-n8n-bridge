"""Сообщение об отказе опроса обязано что-то сообщать (9.11).

Найдено владельцем на живом канале: журнал каждые 30 секунд наполнялся
записями «Ошибка извлечения:» — и всё, после двоеточия пусто. По такой
записи нельзя понять ни что сломалось, ни где искать; смотреть приходится
в логи процесса, до которых у пользователя доступа нет вовсе.

Пусто получается закономерно: `f"Ошибка извлечения: {exc}"` для исключений
без сообщения даёт пустую строку. А без сообщения приходят как раз самые
частые сетевые отказы — `ConnectionError()`, `asyncio.TimeoutError()`,
`asyncio.CancelledError()`. То есть чем типичнее отказ, тем бесполезнее
запись.

Поэтому в сообщении всегда есть ТИП исключения: он есть у любого отказа,
и по нему уже видно направление — сеть, авторизация, отсутствующий канал.
"""

import asyncio

import pytest
from sqlalchemy import select
from test_58_worker_body import FakeTelegram, _monitor, _utc, _worker

from app.models import LogEntry

pytestmark = pytest.mark.asyncio


async def _poll_errors(db) -> list[LogEntry]:
    rows = list(
        await db.scalars(select(LogEntry).where(LogEntry.event_type == "POLL_ERROR"))
    )
    return rows


@pytest.mark.parametrize(
    "failure",
    [ConnectionError(), asyncio.TimeoutError(), ValueError()],
    ids=["ConnectionError", "TimeoutError", "ValueError"],
)
async def test_error_without_a_message_still_names_its_type(db, user, failure):
    """Отказ без текста — самый частый, и он не должен быть немым."""
    await _monitor(db, user, last_checked=_utc(hours=3))
    worker = _worker(db, telegram=FakeTelegram(fail=failure))

    await worker.run_schedule(db)

    rows = await _poll_errors(db)
    assert rows, "падение опроса вообще не попало в журнал"
    details = rows[0].details
    assert type(failure).__name__ in details, (
        f"запись не называет причину: {details!r} — по ней пользователю "
        "нечего делать, а логи процесса ему недоступны"
    )


async def test_error_with_a_message_keeps_it(db, user):
    """Тип добавляется к тексту, а не вместо него."""
    await _monitor(db, user, last_checked=_utc(hours=3))
    worker = _worker(db, telegram=FakeTelegram(fail=ValueError("канал не найден")))

    await worker.run_schedule(db)

    details = (await _poll_errors(db))[0].details
    assert "канал не найден" in details, f"потерян текст ошибки: {details!r}"
    assert "ValueError" in details


async def test_worker_log_carries_the_reason_too(db, user):
    """В логе процесса причина тоже должна быть: раньше там было только
    «опрос упал», и по нему нельзя отличить сеть от чужого канала.

    Обработчик вешается прямо на логгер `app.worker`, а не через caplog:
    caplog ловит записи через корневой логгер, а на его пути в этом
    проекте стоит установленное затирание секретов и чужие обработчики
    соседних тестов — прогон в CI это и показал, упав там, где локально
    было зелено.
    """
    import io
    import logging

    await _monitor(db, user, last_checked=_utc(hours=3))
    worker = _worker(db, telegram=FakeTelegram(fail=ConnectionError()))

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("app.worker")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        await worker.run_schedule(db)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert "ConnectionError" in buffer.getvalue(), (
        f"лог процесса не называет причину падения опроса: {buffer.getvalue()!r}"
    )
