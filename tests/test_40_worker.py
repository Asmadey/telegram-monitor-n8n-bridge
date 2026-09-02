"""Задача 4.1 — воркер как отдельный процесс.

Разделение ответственности (план):
- web (uvicorn, app.main): HTTP + вход в Telegram короткоживущим
  клиентом (3.3) — НИКОГДА не держит долгоживущих;
- worker (app/worker.py, python -m app.worker): опрос каналов,
  автоочистка — единственный владелец пула долгоживущих клиентов.

Почему это принципиально: server.py:37 — глобальный синглтон client;
второй процесс на одном auth-key → AUTH_KEY_DUPLICATED, Telegram может
убить сессию. До разделения приложение принципиально одно-процессное.

Контракты теста (из плана):
1. app.main не импортирует tg_pool (долгоживущие клиенты — только у
   воркера). Проверка по sys.modules: если web-процесс потянет пул,
   тест увидит модуль в памяти.
2. Воркер корректно завершается по request_stop/SIGTERM, ОТКЛЮЧИВ
   клиентов пула (pool.close) — телеметрия «упал, оставив сессии
   подключёнными» хуже, чем «не работал».
3. Живой subprocess `python -m app.worker` умирает от SIGTERM чисто
   (exit 0), а не убивается ядром (returncode -15).

Тело цикла (опрос мониторов) заполняют задачи 4.2 (атомарная
дедупликация) и 4.3 (jobs); здесь — каркас процесса и жизненный цикл.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.services.tg_pool import TelegramClientPool
from app.worker import Worker

_REPO_ROOT = Path(__file__).resolve().parents[1]


class FakePool:
    """Пул-двойник: нужен только close()/sweep_idle/lock/get."""

    def __init__(self):
        self.close_calls = 0
        self.sweep_calls = 0

    async def close(self):
        self.close_calls += 1

    async def sweep_idle(self) -> int:
        self.sweep_calls += 1
        return 0

    def lock(self, user_id: int):
        return asyncio.Lock()

    async def get(self, user_id: int, session_string: str):
        raise AssertionError("в тесте 4.1 цикл не должен опрашивать Telegram")


def test_web_never_imports_tg_pool(_env):
    """Web-процесс (app.main) не тянет пул долгоживущих клиентов.

    Проверка — В ЧИСТОМ интерпретаторе (subprocess): в живом процессе
    pytest модуль tg_pool уже лежит в sys.modules из test_34 — in-process
    проверка врала бы вечно. Трипваер на будущее: стоит роутеру web-сборки
    импортировать tg_pool («один разок дернуть клиент прямо из запроса»)
    — subprocess это увидит.
    """
    from cryptography.fernet import Fernet

    env = dict(os.environ)
    env.update(
        {
            # валидный тестовый ключ и dummy-URL: web обязан ПОДНЯТЬСЯ,
            # иначе проверяли бы не пул, а стартовый барьер 3.4
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DATABASE_URL": "sqlite+aiosqlite://",
        }
    )
    code = "import app.main, sys; print('app.services.tg_pool' in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"web не поднялся: {proc.stderr[-500:]}"
    assert proc.stdout.strip() == "False", (
        "web-процесс импортировал tg_pool — долгоживущие клиенты "
        "принадлежат ТОЛЬКО воркеру (AUTH_KEY_DUPLICATED)"
    )


@pytest.mark.asyncio
async def test_worker_cycle_stops_on_request_and_disconnects_pool():
    """Цикл воркера: тикает, пока не попросят остановиться; при выходе
    отключает клиентов пула. «Не остановился» или «остановился, оставив
    сессии подключёнными» — оба провала."""
    pool = FakePool()
    ticks: list[float] = []

    async def tick():
        ticks.append(time.monotonic())
        await pool.sweep_idle()

    worker = Worker(pool=pool, tick=tick, tick_interval=0.05)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.25)
    assert len(ticks) >= 2, "воркер не тикает — цикл не работает"
    assert pool.sweep_calls >= 2, "щётка пула не гоняется циклом"

    worker.request_stop()
    # wait_for: не завершился за 2с — TimeoutError (тест падает сам,
    # отдельный assert не нужен)
    await asyncio.wait_for(task, timeout=2.0)
    assert pool.close_calls == 1, (
        f"при остановке пул закрыт {pool.close_calls} раз вместо 1 "
        "(клиенты остались подключёнными)"
    )


@pytest.mark.asyncio
async def test_worker_survives_tick_errors():
    """Упавший тик не роняет процесс: воркер продолжает цикл (порт
    except-ветки background_monitor_worker — сервер живёт дальше)."""
    pool = FakePool()
    ticks = []

    async def broken_tick():
        ticks.append(1)
        raise RuntimeError("упс в тике")

    worker = Worker(pool=pool, tick=broken_tick, tick_interval=0.05)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.25)
    assert len(ticks) >= 2, "воркер умер от первого же исключения в тике"
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert pool.close_calls == 1, "пул не закрыт при остановке после ошибок"


@pytest.mark.asyncio
async def test_worker_pool_is_default_long_lived_pool():
    """Воркер по умолчанию строит НАСТОЯЩИЙ пул (3.5), а не заглушку:
    единственный владелец долгоживущих клиентов в системе."""
    worker = Worker()
    assert isinstance(worker.pool, TelegramClientPool), (
        "воркер без явного пула не строит TelegramClientPool"
    )


async def _prepare_worker_db(db_path: Path) -> None:
    """Схема во временной SQLite: воркер открывает БД при старте,
    пустые таблицы — тик не опрашивает ничего (Telegram не трогаем)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_sigterm_exits_cleanly(tmp_path):
    """Живой subprocess: SIGTERM → exit 0 (graceful), не -15 (убит ядром).
    ENV подменяется целиком: ключ шифрования — случайный тестовый,
    DATABASE_URL — временная SQLite со схемой."""
    from cryptography.fernet import Fernet

    db_path = tmp_path / "worker.db"
    await _prepare_worker_db(db_path)

    env = dict(os.environ)
    env.update(
        {
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "SECRET_KEY": "worker-test",
            "ENVIRONMENT": "development",
            "MAIL_DEV_DIR": str(tmp_path / "mail"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # ждём ПЕРВУЮ строку stdout (готовность) с таймаутом — readline
        # блокирующий, поэтому через to_thread + wait_for; без flush у
        # воркера (piped stdout буферизуется) строка не приедет никогда
        # (ловушка, пойманная красной фазой: readline висел вечно)
        try:
            first_line = await asyncio.wait_for(
                asyncio.to_thread(proc.stdout.readline), timeout=20.0
            )
        except asyncio.TimeoutError:
            first_line = ""
        assert first_line and (
            "воркер" in first_line.lower() or "worker" in first_line
        ), (
            f"воркер не подал сигнал готовности: line={first_line!r} "
            f"rc={proc.poll()} stderr={await _read_tail(proc)}"
        )

        proc.terminate()  # SIGTERM
        try:
            rc = await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            rc = proc.wait()
            assert False, "воркер не завершился по SIGTERM за 10 секунд"
        assert rc == 0, (
            f"SIGTERM не обработан gracefully: rc={rc} (-15 = убит ядром, "
            f"сессии не отключены) stderr={await _read_tail(proc)}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


async def _read_tail(proc: subprocess.Popen) -> str:
    """Хвост stderr упавшего воркера — для диагностики в assert."""
    try:
        tail = await asyncio.wait_for(asyncio.to_thread(proc.stderr.read), timeout=2.0)
        return tail[-500:]
    except asyncio.TimeoutError:
        return "<stderr недоступен>"
