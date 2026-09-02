"""Пул долгоживущих Telethon-клиентов воркера (задача 3.5 PLAN.md).

Пул живёт ТОЛЬКО в воркере (задача 4.1): web поднимает короткоживущий
клиент на время авторизации (3.3) — два долгоживущих клиента на одном
auth-key это AUTH_KEY_DUPLICATED, Telegram может убить сессию.

Контракты:
- лимит живых клиентов (`limit`, по умолчанию 20) с вытеснением LRU:
  при исчерпании отключается самый давно не запрашиваемый клиент;
- отключение по простою (`idle_timeout`, по умолчанию 10 минут):
  воркер регулярно зовёт sweep_idle() перед циклом опроса;
- lock() — один asyncio.Lock на пользователя: два опроса одного
  аккаунта не идут параллельно. Lock берёт ВЫЗЫВАЮЩИЙ (цикл воркера),
  get() внутрь блокировки не лезет;
- клиент строится фабрикой (в тестах — фейк): пул не знает, как
  устроен Telethon, он только держит/вытесняет/отключает;
- строка сессии передаётся УЖЕ расшифрованной (decrypt — не дело пула).

FloodWait (правило AGENTS.md: НЕ ретраить сразу):
flood_guarded_call ловит FloodWaitError, отдаёт retry_after в
on_flood_wait (монитор запишет) и возвращает None — цикл опроса
пропускается целиком. Guard НИКОГДА не спит: Telethon сам умеет ждать
после FloodWait, но при seconds > 300 это вешает воркер на десятки
минут — потому порог FLOOD_WAIT_SKIP_THRESHOLD и «пропустить цикл»
вместо «подождать».
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Protocol

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from app.config import get_settings

FLOOD_WAIT_SKIP_THRESHOLD = 300


class SupportsLifecycle(Protocol):
    """Поверхность клиента, которую держит пул (Telethon-клиент и фейки)."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...


def _default_client_factory(user_id: int, session_string: str) -> TelegramClient:
    """Живая фабрика: клиент на StringSession (сессия уже расшифрована)."""
    settings = get_settings()
    return TelegramClient(
        StringSession(session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


class _Entry:
    """Живой клиент в пуле + LRU/простой-метрики."""

    __slots__ = ("client", "session_string", "last_used")

    def __init__(self, client: SupportsLifecycle, session_string: str, now: float):
        self.client = client
        self.session_string = session_string
        self.last_used = now


class TelegramClientPool:
    def __init__(
        self,
        *,
        client_factory: Callable[
            [int, str], SupportsLifecycle
        ] = _default_client_factory,
        limit: int = 20,
        idle_timeout: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._factory = client_factory
        self._limit = limit
        self._idle_timeout = idle_timeout
        self._clock = clock
        # OrderedDict: порядок = LRU (слева самые «холодные»)
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._locks: dict[int, asyncio.Lock] = {}

    def size(self) -> int:
        """Живые клиенты в пуле (для мониторинга и тестов)."""
        return len(self._entries)

    def lock(self, user_id: int) -> asyncio.Lock:
        """Один lock на пользователя — опросы одного аккаунта сериализуются."""
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    async def get(self, user_id: int, session_string: str):
        """Клиент пользователя: живой — переиспользуется, иначе создаётся.

        Сессия изменилась (перелогин) — старый клиент отключается,
        поднимается новый: опрашивать мёртвой сессией нельзя.
        """
        now = self._clock()
        entry = self._entries.get(user_id)
        if entry is not None and entry.session_string == session_string:
            entry.last_used = now
            self._entries.move_to_end(user_id)  # LRU: стал «горячим»
            return entry.client

        if entry is not None:
            await self._drop(user_id)  # старая сессия: клиента — вон

        client = self._factory(user_id, session_string)
        await client.connect()
        self._entries[user_id] = _Entry(client, session_string, now)
        await self._evict_over_limit()
        return client

    async def _drop(self, user_id: int) -> None:
        entry = self._entries.pop(user_id, None)
        if entry is not None:
            await entry.client.disconnect()

    async def _evict_over_limit(self) -> None:
        """Лимит пробит — отключаем самых «холодных» (LRU, голова OrderedDict)."""
        while len(self._entries) > self._limit:
            user_id, _ = next(iter(self._entries.items()))
            await self._drop(user_id)

    async def sweep_idle(self) -> int:
        """Отключить клиентов, простаивающих дольше idle_timeout.

        Щётку гоняет воркер перед циклом; disconnect ждём ДО возврата —
        fire-and-forget здесь утечка (клиент обязан умереть до следующего
        опроса). Возвращает число отключённых.
        """
        now = self._clock()
        stale = [
            user_id
            for user_id, entry in self._entries.items()
            if now - entry.last_used > self._idle_timeout
        ]
        for user_id in stale:
            entry = self._entries.pop(user_id)
            await entry.client.disconnect()
        return len(stale)

    async def close(self) -> None:
        """Отключить всех (SIGTERM воркера) — пул обязан умереть пустым."""
        entries = list(self._entries.items())
        self._entries.clear()
        for _, entry in entries:
            await entry.client.disconnect()


async def flood_guarded_call(
    call: Callable[[], Awaitable],
    *,
    on_flood_wait: Callable[[errors.FloodWaitError], Awaitable],
):
    """Выполнить опрос; FloodWaitError — не ретраить и не ронять цикл.

    Возвращает None при FloodWait (цикл пропускается), retry_after —
    в on_flood_wait. НИКОГДА не спит: ждать inline = повесить воркера
    (FloodWait бывает на тысячи секунд).
    """
    try:
        return await call()
    except errors.FloodWaitError as e:
        await on_flood_wait(e)
        return None
