"""Задача 3.5 — пул Telethon-клиентов воркера.

Пул живёт ТОЛЬКО в воркере (4.1): web поднимает короткоживущие клиенты
(3.3), долгоживущие на два процесса — AUTH_KEY_DUPLICATED.

Контракты плана:
- лимит одновременно живых клиентов (~20), вытеснение по LRU;
- отключение по простою (~10 минут без запросов);
- asyncio.Lock на пользователя: два опроса одного аккаунта не параллелятся;
- FloodWaitError не ретраится и не роняет цикл: retry_after записывается
  в монитор, цикл пропускается. Telethon сам умеет ждать, но при
  seconds > 300 это вешает воркер — ждём только «вне цикла», никогда
  не спим внутри guard'а.

Telethon подменён фейком: тесты не ходят в живой Telegram.
"""

import asyncio

import pytest
from telethon import errors

from app.services.tg_pool import (
    FLOOD_WAIT_SKIP_THRESHOLD,
    TelegramClientPool,
    flood_guarded_call,
)

IDLE_TIMEOUT = 600.0  # 10 минут (план)


class FakeClient:
    """Поверхность Telethon, нужная пулу: connect/disconnect."""

    def __init__(self, label: str):
        self.label = label
        self.connected = False
        self.disconnect_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


class ClientFactory:
    """Фабрика-счётчик: пул обязан строить клиентов только через неё."""

    def __init__(self):
        self.created: list[FakeClient] = []

    def __call__(self, user_id: int, session_string: str) -> FakeClient:
        client = FakeClient(f"client-{len(self.created)}")
        self.created.append(client)
        return client


def _pool(limit: int = 20, clock=None) -> tuple[TelegramClientPool, ClientFactory]:
    factory = ClientFactory()
    pool = TelegramClientPool(
        client_factory=factory,
        limit=limit,
        idle_timeout=IDLE_TIMEOUT,
        clock=clock or (lambda: 0.0),
    )
    return pool, factory


@pytest.mark.asyncio
async def test_pool_never_exceeds_limit():
    """5 пользователей при лимите 3: в пуле не больше 3 живых клиентов,
    вытесненные (LRU — самые старые) отключены."""
    pool, factory = _pool(limit=3)
    for uid in range(5):
        await pool.get(uid, f"sess-{uid}")
    assert pool.size() <= 3, f"лимит пробит: {pool.size()} живых клиентов при лимите 3"
    # вытеснены САМЫЕ СТАРЫЕ (LRU): клиенты 0 и 1 отключены
    assert factory.created[0].disconnect_calls == 1, "LRU-жертва не отключена"
    assert factory.created[1].disconnect_calls == 1, "LRU-жертва не отключена"
    # живые — подключены
    assert all(c.connected for c in factory.created[2:])


@pytest.mark.asyncio
async def test_same_user_reuses_client():
    """Повторный запрос того же пользователя переиспользует клиента:
    фабрика вызвана один раз, клиент — тот же объект."""
    pool, factory = _pool()
    c1 = await pool.get(7, "sess-a")
    c2 = await pool.get(7, "sess-a")
    assert c1 is c2, "пул создал второго клиента для того же пользователя"
    assert len(factory.created) == 1, "фабрика вызвана больше одного раза"


@pytest.mark.asyncio
async def test_changed_session_replaces_client():
    """Пользователь перелогинился (строка сессии изменилась): старый
    клиент отключается, поднимается новый — иначе воркер опрашивает
    мёртвой сессией."""
    pool, factory = _pool()
    c1 = await pool.get(7, "sess-a")
    c2 = await pool.get(7, "sess-b")
    assert c1 is not c2, "пул отдал клиента со старой сессией"
    assert c1.disconnect_calls == 1, "клиент со старой сессией не отключен"


@pytest.mark.asyncio
async def test_idle_client_disconnected_by_sweep():
    """Клиент без запросов дольше idle_timeout отключается щёткой;
    свежий — остаётся жить."""
    clock_vals = {"now": 0.0}
    pool, factory = _pool(clock=lambda: clock_vals["now"])

    idle_client = await pool.get(1, "sess-1")
    clock_vals["now"] = 1.0
    await pool.get(2, "sess-2")  # свежий: последний запрос в t=1

    # t = 1 + IDLE_TIMEOUT: клиент 1 простаивает дольше лимита (601 > 600),
    # клиент 2 — ровно на границе (600), границу НЕ считаем простоем.
    # (Ошибка первой версии теста: щётка на 602 зацепила обоих.)
    clock_vals["now"] = 1.0 + IDLE_TIMEOUT
    reaped = await pool.sweep_idle()
    assert reaped == 1, f"щётка отключила {reaped} клиентов вместо 1"
    assert idle_client.disconnect_calls == 1, "залежавшийся клиент жив"
    assert pool.size() == 1, "щётка не убрала строку из пула"
    # свежий не тронут
    remaining = await pool.get(2, "sess-2")
    assert remaining.connected, "свежий клиент отключён щёткой"


def test_lock_is_per_user_and_reusable():
    """Lock один на пользователя: два опроса одного аккаунта
    сериализуются через ОДИН объект блокировки."""
    pool, _ = _pool()
    l1 = pool.lock(42)
    l2 = pool.lock(42)
    l3 = pool.lock(43)
    assert l1 is l2, "каждый вызов давал НОВЫЙ lock — сериализации нет"
    assert l1 is not l3, "lock общий для всех пользователей"
    assert isinstance(l1, asyncio.Lock)


def _flood_wait(seconds: int) -> errors.FloodWaitError:
    """FloodWaitError с заданным seconds (capture в терминах Telethon)."""
    exc = errors.FloodWaitError(request=None, capture=seconds)
    return exc


@pytest.mark.asyncio
async def test_flood_wait_does_not_crash_cycle_and_never_sleeps():
    """FloodWaitError: цикл НЕ падает (вызов возвращает None), retry_after
    уезжает в on_flood_wait (монитор запишет), guard НИКОГДА не спит —
    даже при seconds > 300 (иначе воркер висит на FloodWait часами)."""
    recorded: list[errors.FloodWaitError] = []

    async def poll_that_floods():
        raise _flood_wait(600)  # > 300: Telethon сам бы ждал 10 минут

    async def on_flood_wait(exc):
        recorded.append(exc)

    loop = asyncio.get_event_loop()
    started = loop.time()
    result = await flood_guarded_call(poll_that_floods, on_flood_wait=on_flood_wait)
    elapsed = loop.time() - started

    assert result is None, "FloodWait пробил guard и уронил цикл"
    assert len(recorded) == 1 and recorded[0].seconds == 600, (
        "retry_after не передан монитору"
    )
    assert elapsed < 1.0, f"guard спал внутри вызова: {elapsed:.1f}s"


def test_flood_wait_threshold_is_300_seconds():
    """Порог «дольше этого — пропустить цикл, не ждать» — 300 секунд."""
    assert FLOOD_WAIT_SKIP_THRESHOLD == 300


@pytest.mark.asyncio
async def test_close_disconnects_everyone():
    """close() (SIGTERM воркера) отключает всех клиентов и опустошает пул."""
    pool, factory = _pool()
    clients = [await pool.get(uid, f"sess-{uid}") for uid in range(3)]
    await pool.close()
    assert all(c.disconnect_calls == 1 for c in clients), (
        "не все клиенты отключены при close"
    )
    assert pool.size() == 0, "пул не опустошён при close"
