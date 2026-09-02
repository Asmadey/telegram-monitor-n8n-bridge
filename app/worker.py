"""Воркер — отдельный процесс-владелец долгоживущих Telethon-клиентов
(задача 4.1 PLAN.md).

Разделение ответственности (проблема server.py:37 — глобальный синглтон
client: второй процесс на одном auth-key → AUTH_KEY_DUPLICATED):

| процесс | делает                                     | Telethon                       |
|---------|--------------------------------------------|--------------------------------|
| web     | HTTP, логин, вход в Telegram (3.3)          | короткоживущий клиент на запрос |
| worker  | опрос каналов, автоочистка (4.x, Фаза 5)   | пул долгоживущих (3.5)          |

Оба процесса — из одного Docker-образа, разные команды: web — uvicorn
app.main:app, воркер — python -m app.worker.

Жизненный цикл: request_stop() (SIGTERM/SIGINT) → цикл выходит, пул
закрывает ВСЕХ клиентов — телеметрия «упал, оставив чужие сессии
подключёнными» хуже, чем «не работал». Упавший тик не роняет процесс
(порт except-ветки background_monitor_worker, server.py:616).

Тело цикла — каркас: тик = щётка пула; опрос мониторов заполняют
задачи 4.2 (атомарная дедупликация) и 4.3 (jobs), диспетчеризация —
Фаза 5. Воркер НЕ импортируется web-сборкой (test_40 держит).
"""

import asyncio
import logging
import signal

from app.db import get_sessionmaker
from app.security.crypto import validate_encryption_key
from app.services.tg_pool import TelegramClientPool

logger = logging.getLogger(__name__)

# между тиками (порт background_monitor_worker: там 30 секунд)
TICK_INTERVAL = 30.0


class Worker:
    """Цикл воркера. tick инъектируется — реальное тело опроса доращивается
    задачами 4.2/4.3 без смены каркаса жизненного цикла."""

    def __init__(
        self,
        *,
        pool: TelegramClientPool | None = None,
        tick=None,
        tick_interval: float = TICK_INTERVAL,
    ):
        self.pool = pool if pool is not None else TelegramClientPool()
        self._tick = tick if tick is not None else self._default_tick
        self._tick_interval = tick_interval
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """SIGTERM/SIGINT: цикл выходит после текущего тика."""
        self._stop.set()

    async def _default_tick(self) -> None:
        """Тик по умолчанию: щётка пула (простой > 10 мин → disconnect).
        Тело опроса мониторов — задачи 4.2/4.3."""
        reaped = await self.pool.sweep_idle()
        if reaped:
            logger.info("щётка отключила %d клиентов по простою", reaped)

    async def run(self) -> None:
        """Цикл до request_stop; в finally — close пула ВСЕГДА (и при
        падении тика тоже): чужие MTProto-сессии нельзя бросать живыми."""
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — процесс живёт дальше
                    logger.exception("тик воркера упал, продолжаю цикл")
                # спим, но просыпаемся от SIGTERM немедленно
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._tick_interval
                    )
                except TimeoutError:
                    pass
        finally:
            await self.pool.close()
            logger.info("воркер остановлен, пул закрыт")


async def _amain() -> None:
    # стартовые барьеры те же, что у web (3.4): без ключа шифрования
    # воркер не расшифрует сессии; без базы нечего опрашивать.
    validate_encryption_key()
    get_sessionmaker()  # громкий отказ без DATABASE_URL (урок 2026-09-02)

    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    # flush=True: piped stdout буферизуется — супервайзер (и тест)
    # ждут строку готовности, без flush она не приедет до смерти процесса
    print("воркер стартовал: цикл опроса, SIGTERM — graceful shutdown", flush=True)
    logger.info("воркер запущен, интервал тика %.0fs", TICK_INTERVAL)
    await worker.run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
