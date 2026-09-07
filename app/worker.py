"""Воркер — отдельный процесс-владелец долгоживущих Telethon-клиентов
(задача 4.1 PLAN.md) и единственный исполнитель работы.

Разделение ответственности (проблема server.py:37 — глобальный синглтон
client: второй процесс на одном auth-key → AUTH_KEY_DUPLICATED):

| процесс | делает                                     | Telethon                       |
|---------|--------------------------------------------|--------------------------------|
| web     | HTTP, логин, вход в Telegram (3.3)          | короткоживущий клиент на запрос |
| worker  | очередь, опрос каналов, доставка, очистка   | пул долгоживущих (3.5)          |

Оба процесса — из одного Docker-образа, разные команды: web — uvicorn
app.main:app, воркер — python -m app.worker.

Жизненный цикл: request_stop() (SIGTERM/SIGINT) → цикл выходит, пул
закрывает ВСЕХ клиентов — телеметрия «упал, оставив чужие сессии
подключёнными» хуже, чем «не работал». Упавший тик не роняет процесс
(порт except-ветки background_monitor_worker, server.py:616).

**Тик — четыре шага, в этом порядке:**

1. вернуть в очередь задачи, брошенные умершим процессом;
2. разобрать очередь `jobs` (ручной запуск из интерфейса);
3. опросить мониторы, у которых истёк интервал;
4. автоочистка — не чаще раза в сутки на пользователя.

**Тенантность.** Задача берётся из очереди БЕЗ фильтра по `user_id` —
воркер обслуживает всех. Всё дальнейшее идёт по `user_id` ИЗ СТРОКИ
ЗАДАЧИ: это единственное место в проекте, где владелец данных приходит не
из сессии пользователя, и ошибка здесь означает чужие посты в чужом
вебхуке. Поэтому монитор ищется по паре (user_id, public_id), а не по
одному public_id — он уникален только в пределах пользователя.

Граница с Telegram — `TelegramGateway`: в тестах он заменяется двойником
целиком, всё остальное гоняется по-настоящему.
"""

import asyncio
import datetime
import json
import logging
import signal
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import TenantRepo, get_sessionmaker
from app.models import FeedItem, Integration, Job, Monitor, TelegramAccount
from app.security.crypto import validate_encryption_key
from app.security.log_redaction import install_log_redaction
from app.services.cleanup import purge_older_than
from app.services.dedup import filter_new
from app.services.dispatch import dispatch, store_avatar
from app.services.jobs import (
    claim_next_job,
    fail_job,
    finish_job,
    requeue_hung_jobs,
)
from app.services.journal import add_log, redact
from app.services.llm import process_messages_batch_with_llm
from app.services.ops import WORKER_NAME, record_heartbeat
from app.services.tg_gateway import TelegramGateway
from app.services.tg_pool import TelegramClientPool, flood_guarded_call

logger = logging.getLogger(__name__)

# между тиками (порт background_monitor_worker: там 30 секунд)
TICK_INTERVAL = 30.0

# Потолок задач за тик. Без него один пользователь, поставивший сотню
# ручных запусков, откладывает расписание всех остальных на неопределённое
# время: очередь разбирается до дна раньше, чем цикл дойдёт до шага 3.
MAX_JOBS_PER_TICK = 20

# автоочистка — раз в сутки (порт server.py:527)
CLEANUP_INTERVAL = 86400.0

# длина текста ошибки в jobs.error: колонка видна в интерфейсе
MAX_ERROR_CHARS = 1000

KIND_POLL = "poll_monitor"
KIND_REANALYZE = "reanalyze_feed_item"
KIND_BATCH = "process_batch"
RETRY_SECONDS = 60
LEADER_RETRY_INTERVAL = 5.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime | None) -> datetime.datetime | None:
    """Привести время из БД к aware.

    SQLite возвращает datetime БЕЗ tzinfo, Postgres — с ним. Арифметика
    naive и aware бросает TypeError, то есть один и тот же код падал бы
    только в одной из двух сред — ровно тот класс расхождений, который
    ловится не тестами, а инцидентом в проде.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _retry_deadline(exc: Exception) -> datetime.datetime:
    deadline = getattr(exc, "retry_after", None)
    if isinstance(deadline, datetime.datetime):
        return _aware(deadline) or deadline
    return _utcnow() + datetime.timedelta(seconds=RETRY_SECONDS)


class JobDeferred(Exception):
    """Задача из очереди не может выполниться раньше внешнего срока
    (FloodWait, отсрочка провайдера) — она откладывается, а не падает."""

    def __init__(self, retry_after: datetime.datetime):
        super().__init__("job deferred")
        self.retry_after = retry_after


class Worker:
    """Цикл воркера. Тик по умолчанию — реальная работа; `tick`
    инъектируется тестами жизненного цикла (4.1)."""

    def __init__(
        self,
        *,
        pool: TelegramClientPool | None = None,
        tick=None,
        tick_interval: float = TICK_INTERVAL,
        sessionmaker=None,
        telegram=None,
        dispatcher=None,
    ):
        self.pool = pool if pool is not None else TelegramClientPool()
        self._tick = tick if tick is not None else self._default_tick
        self._tick_interval = tick_interval
        # сессиймейкер разрешается лениво: Worker() строится и там, где
        # DATABASE_URL ещё не задан (тест 4.1 на тип пула)
        self._sessionmaker = sessionmaker
        self.telegram = telegram if telegram is not None else TelegramGateway(self.pool)
        self._dispatch = dispatcher if dispatcher is not None else dispatch
        self.llm = process_messages_batch_with_llm
        self._stop = asyncio.Event()
        self._leadership: Callable[[], Awaitable[bool]] | None = None

    def request_stop(self) -> None:
        """SIGTERM/SIGINT: цикл выходит после текущего тика."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Тик
    # ------------------------------------------------------------------

    async def _default_tick(self) -> None:
        reaped = await self.pool.sweep_idle()
        if reaped:
            logger.info("щётка отключила %d клиентов по простою", reaped)

        maker = self._sessionmaker or get_sessionmaker()
        async with maker() as db:
            # Отметка живости — первым делом в тике (10.2): по ней снаружи
            # видно, что воркер не просто запущен, а доходит до работы.
            # leader=True не допущение: цикл вызывает тик только после
            # успешной проверки лидерства (или когда её нет вовсе — один
            # процесс). Отметиться, не будучи лидером, здесь нельзя.
            await record_heartbeat(db, WORKER_NAME, leader=True)
            await requeue_hung_jobs(db)
            await self.run_jobs(db)
            await self.run_schedule(db)
            await self.run_cleanup(db)

    # ------------------------------------------------------------------
    # 1–2. Очередь
    # ------------------------------------------------------------------

    async def run_jobs(self, db) -> int:
        """Разобрать очередь. Падение задачи — failed и следующая: один
        сломанный канал не останавливает остальных пользователей."""
        done = 0
        for _ in range(MAX_JOBS_PER_TICK):
            job = await claim_next_job(db)
            if job is None:
                break
            # id и вид запоминаются ДО выполнения: rollback ниже обесценивает
            # (expire) ORM-объект, и любое обращение к его полю после отката
            # — это ленивая загрузка, то есть MissingGreenlet в async-сессии
            job_id, job_kind = job.id, job.kind
            try:
                await self._run_job(db, job)
            except JobDeferred as deferred:
                await db.rollback()
                stale = await db.get(Job, job_id)
                if stale is not None:
                    stale.status = "pending"
                    stale.started_at = None
                    stale.finished_at = None
                    stale.retry_after = deferred.retry_after
                    stale.error = None
                    await db.commit()
            except Exception as exc:  # noqa: BLE001 — очередь живёт дальше
                await db.rollback()
                stale = await db.get(Job, job_id)
                if stale is not None:
                    # redact: текст исключения httpx несёт Authorization:
                    # Bearer, а jobs.error виден в интерфейсе — второй сток
                    # для секретов после журнала (4.6)
                    if job_kind == KIND_BATCH:
                        stale.status = "pending"
                        stale.started_at = None
                        stale.retry_after = _retry_deadline(exc)
                        stale.error = redact(str(exc))[:MAX_ERROR_CHARS]
                        await db.commit()
                    else:
                        await fail_job(
                            db, stale, error=redact(str(exc))[:MAX_ERROR_CHARS]
                        )
                logger.warning(
                    "задача %s (%s) упала: %s", job_id, job_kind, redact(str(exc))
                )
            else:
                await finish_job(db, job)
                done += 1
        return done

    async def _run_job(self, db, job) -> None:
        try:
            payload = json.loads(job.payload_json or "{}")
        except ValueError as exc:
            raise ValueError(f"повреждённый payload задачи: {exc}") from exc

        if job.kind == KIND_BATCH:
            result = await self._dispatch(
                db,
                job.user_id,
                payload["batch"],
                channel_prompt=payload.get("prompt"),
            )
            if result.get("retry"):
                raise RuntimeError(
                    "Delivery temporarily failed; saved result awaits retry"
                )
        elif job.kind == KIND_POLL:
            # Владелец берётся из строки задачи — единственное место
            # проекта, где он не из сессии; фильтр всё равно ставит репозиторий.
            # public_id уникален только в пределах пользователя, поэтому один
            # он бы указал на чужой канал.
            monitor = (
                await db.scalars(
                    TenantRepo(db, job.user_id)
                    .query(Monitor)
                    .where(Monitor.public_id == payload.get("monitor_public_id"))
                )
            ).first()
            if monitor is None:
                raise LookupError("монитор не найден у владельца задачи")
            result = await self.poll_monitor(db, monitor)
            if result == "flood_wait":
                account = (
                    await db.scalars(TenantRepo(db, job.user_id).query(TelegramAccount))
                ).first()
                retry_after = _aware(account.retry_after) if account else None
                raise JobDeferred(
                    retry_after
                    or (_utcnow() + datetime.timedelta(seconds=RETRY_SECONDS))
                )
        elif job.kind == KIND_REANALYZE:
            await self._reanalyze(db, job.user_id, payload.get("feed_item_id"))
        else:
            raise ValueError(f"неизвестный вид задачи: {job.kind}")

    async def _reanalyze(self, db, user_id: int, feed_item_id) -> None:
        item = (
            await db.scalars(
                TenantRepo(db, user_id)
                .query(FeedItem)
                .where(FeedItem.id == feed_item_id)
            )
        ).first()
        if item is None:
            raise LookupError("запись ленты не найдена у владельца задачи")
        messages = json.loads(item.raw_messages_json or "[]")
        if not messages:
            raise ValueError("в записи нет исходных постов")
        analysis = await self.llm(db, user_id, messages, require_success=True)
        if analysis:
            item.ai_analysis = analysis
            await db.commit()

    # ------------------------------------------------------------------
    # 3. Расписание
    # ------------------------------------------------------------------

    def _is_due(self, monitor: Monitor, now: datetime.datetime) -> bool:
        last = _aware(monitor.last_checked)
        if last is None:  # только что добавленный канал — опрос сразу
            return True
        return now >= last + datetime.timedelta(minutes=monitor.interval_minutes)

    async def run_schedule(self, db) -> int:
        now = _utcnow()
        monitors = list(
            await db.scalars(select(Monitor.id).where(Monitor.is_active.is_(True)))
        )
        polled = 0
        for monitor_id in monitors:
            monitor = await db.get(Monitor, monitor_id, populate_existing=True)
            if (
                monitor is None
                or not monitor.is_active
                or not self._is_due(monitor, now)
            ):
                continue
            # Поля запоминаются ДО опроса: rollback в ветке ошибки обесценивает
            # (expire) ORM-объект, и обращение к monitor.user_id после отката —
            # это ленивая загрузка, то есть MissingGreenlet в async-сессии.
            # Тот же корень, что и у задач очереди выше.
            owner_id = monitor.user_id
            chat_id, chat_title = monitor.chat_id, monitor.chat_title
            public_id = monitor.public_id
            try:
                await self.poll_monitor(db, monitor)
                polled += 1
            except Exception as exc:  # noqa: BLE001 — один канал не роняет цикл
                await db.rollback()
                await add_log(
                    db,
                    owner_id,
                    "POLL_ERROR",
                    f"Ошибка извлечения: {exc}",
                    status="ERROR",
                    chat_id=chat_id,
                    chat_title=chat_title,
                )
                logger.warning(
                    "монитор %s тенанта %s: опрос упал",
                    public_id,
                    owner_id,
                    # Do not attach raw exception tracebacks containing credentials.
                )
        return polled

    async def poll_monitor(self, db, monitor: Monitor) -> str:
        """Опрос одного канала: выборка → дедупликация → доставка.

        Возвращает исход строкой (для диагностики и тестов).
        """
        user_id = monitor.user_id
        account = (
            await db.scalars(TenantRepo(db, user_id).query(TelegramAccount))
        ).first()
        account_retry = _aware(account.retry_after) if account is not None else None
        if account_retry is not None and _utcnow() < account_retry:
            return "flood_wait"

        async def _flood(exc) -> None:
            if account is not None:
                account.retry_after = _utcnow() + datetime.timedelta(
                    seconds=max(1, exc.seconds)
                )
            await db.commit()
            await add_log(
                db,
                user_id,
                "FLOOD_WAIT",
                f"Telegram просит подождать {getattr(exc, 'seconds', '?')} с — "
                f"опрос «{monitor.chat_title or monitor.chat_target}» пропущен",
                status="SKIPPED",
                chat_id=monitor.chat_id,
                chat_title=monitor.chat_title,
            )

        async def _work():
            client = await self.telegram.client_for(db, user_id)
            if client is None:
                return None, None, None
            entity = await self.telegram.resolve(client, monitor.chat_target)
            messages = await self.telegram.fetch(
                client,
                entity,
                limit=monitor.limit_count,
                offset_hours=monitor.offset_hours,
            )
            return client, entity, messages

        # FloodWaitError не ретраится и не спит inline: ожидание на тысячи
        # секунд повесило бы воркера целиком, то есть всех пользователей
        result = await flood_guarded_call(_work, on_flood_wait=_flood)
        if result is None:
            return "flood_wait"
        client, entity, messages = result
        if client is None:
            return "no_account"

        # chat_id — ключ дедупликации. Без него посты всех каналов легли бы
        # под одним ключом 0: разные каналы начали бы «глушить» друг друга,
        # и это выглядело бы как «канал перестал присылать новое».
        resolved = monitor.chat_id or getattr(entity, "id", None)
        if not resolved:
            raise LookupError(f"канал {monitor.chat_target} не дал chat_id")
        chat_id = int(resolved)
        monitor.chat_id = chat_id
        monitor.chat_title = getattr(entity, "title", None) or monitor.chat_title
        monitor.chat_username = (
            getattr(entity, "username", None) or monitor.chat_username
        )
        monitor.last_checked = _utcnow()
        if account is not None:
            account.retry_after = None
        await db.commit()

        # Reserve IDs and their full retry payload in ONE transaction. A crash
        # never leaves a reservation without recoverable source messages.
        fresh = await filter_new(
            db, user_id, chat_id, messages, commit=False, processed=False
        )
        if not fresh:
            await add_log(
                db,
                user_id,
                "SCHEDULER_POLL",
                f"Опрос завершён: {len(messages)} сообщений, новых нет.",
                status="SKIPPED_DEDUP",
                chat_id=chat_id,
                chat_title=monitor.chat_title,
            )
            return "no_new"
        batch = {
            "job_id": str(uuid.uuid4()),
            "chat_id": chat_id,
            "chat_title": monitor.chat_title,
            "chat_username": monitor.chat_username or "",
            "messages_count": len(fresh),
            "messages": fresh,
        }
        job = Job(
            user_id=user_id,
            kind=KIND_BATCH,
            payload_json=json.dumps(
                {"batch": batch, "prompt": monitor.prompt}, ensure_ascii=False
            ),
            status="running",
            started_at=_utcnow(),
        )
        db.add(job)
        await db.commit()
        job_id = job.id
        try:
            # Avatar failure is cosmetic and must not prevent batch processing.
            try:
                avatar = await self.telegram.avatar(client, entity)
                if avatar:
                    await store_avatar(db, chat_id, avatar)
            except Exception as exc:
                logger.warning("avatar unavailable: %s", redact(str(exc)))
                await db.rollback()
                job = await db.get(Job, job_id)
            await self._run_job(db, job)
            await finish_job(db, job)
        except Exception as exc:
            await db.rollback()
            job = await db.get(Job, job_id)
            job.status = "pending"
            job.started_at = None
            job.error = redact(str(exc))[:MAX_ERROR_CHARS]
            job.retry_after = _retry_deadline(exc)
            await db.commit()
            logger.warning("batch %s awaiting retry: %s", job_id, redact(str(exc)))
        return "dispatched"

    # ------------------------------------------------------------------
    # 4. Автоочистка
    # ------------------------------------------------------------------

    async def run_cleanup(self, db) -> int:
        """Суточная автоочистка по каждому включившему её пользователю."""
        now = _utcnow()
        rows = list(
            await db.scalars(
                select(Integration).where(Integration.cleanup_enabled.is_(True))
            )
        )
        cleaned = 0
        for row in rows:
            last = _aware(row.cleanup_last_run)
            if last is not None and (now - last).total_seconds() < CLEANUP_INTERVAL:
                continue
            removed = await purge_older_than(db, row.user_id, row.cleanup_days)
            row.cleanup_last_run = now
            await db.commit()
            await add_log(
                db,
                row.user_id,
                "AUTO_CLEANUP",
                f"Автоочистка старше {row.cleanup_days} дн.: "
                f"логи {removed['logs']}, сообщения {removed['messages']}, "
                f"лента {removed['feed']}",
                status="SUCCESS",
            )
            cleaned += 1
        return cleaned

    # ------------------------------------------------------------------
    # Цикл
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Цикл до request_stop; в finally — close пула ВСЕГДА (и при
        падении тика тоже): чужие MTProto-сессии нельзя бросать живыми."""
        try:
            while not self._stop.is_set():
                # Guard failures terminate the process and close Telegram clients.
                # They must not be swallowed as recoverable poll errors.
                if self._leadership is not None and not await self._leadership():
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self._tick_interval
                        )
                    except TimeoutError:
                        pass
                    continue
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — процесс живёт дальше
                    logger.warning("тик воркера упал: %s", redact(str(exc)))
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
    install_log_redaction()  # 9.4: секрет не уйдёт в stdout ни из чьего лога
    get_sessionmaker()  # громкий отказ без DATABASE_URL (урок 2026-09-02)

    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    # flush=True: piped stdout буферизуется — супервайзер (и тест)
    # ждут строку готовности, без flush она не приедет до смерти процесса
    print("воркер стартовал: цикл опроса, SIGTERM — graceful shutdown", flush=True)
    logger.info("воркер запущен, интервал тика %.0fs", TICK_INTERVAL)
    maker = get_sessionmaker()
    async with maker() as db:
        engine = db.bind
        if not isinstance(engine, AsyncEngine):
            await worker.pool.close()
            raise RuntimeError("worker database is not an async engine")
        if engine.dialect.name == "postgresql":
            # Dedicated connection owns a session advisory lock throughout the
            # Telegram pool lifetime. Standby deployments do not connect clients.
            async with engine.connect() as connection:
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                leadership = PostgresLeadership(connection)
                acquired = await leadership.acquire(
                    worker._stop, retry_interval=LEADER_RETRY_INTERVAL
                )
                if not acquired:
                    await worker.pool.close()
                    return
                worker._leadership = leadership.check
                try:
                    await worker.run()
                finally:
                    try:
                        await leadership.release()
                    except Exception:  # noqa: BLE001 - invalidation releases lock
                        logger.warning("не удалось явно освободить leader lock")
                    finally:
                        # Discard physical connection; a pooled session must never
                        # retain our advisory lock after this process stops.
                        await connection.invalidate()
        else:
            from app.config import get_settings

            if get_settings().is_production:
                await worker.pool.close()
                raise RuntimeError(
                    "production worker requires PostgreSQL advisory leader lock"
                )
            logger.warning(
                "SQLite worker has no cross-process leader guarantee; "
                "development and tests only"
            )
            await worker.run()


class PostgresLeadership:
    """Один активный воркер на базу. Потеря соединения трактуется как
    потеря лидерства: отказ в сторону остановки, а не двойной работы."""

    LOCK_ID = 846352910

    def __init__(self, connection):
        self.connection = connection
        self.acquired = False

    async def acquire(
        self, stop: asyncio.Event, *, retry_interval: float = LEADER_RETRY_INTERVAL
    ) -> bool:
        """Дождаться исключительного владения — до этого Worker.run не стартует.

        Блокировка привязана к этому выделенному соединению PostgreSQL.
        Поэтому при выкатке новый процесс простаивает, пока старый воркер
        не отключит все Telegram-клиенты и не отпустит блокировку — двух
        владельцев одного auth-key не возникает даже на секунду.
        """
        while not stop.is_set():
            self.acquired = bool(
                await self.connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self.LOCK_ID}
                )
            )
            if self.acquired:
                return True
            if retry_interval <= 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=retry_interval)
            except TimeoutError:
                pass
        return False

    async def check(self) -> bool:
        if not self.acquired:
            return False
        await self.connection.execute(text("SELECT 1"))
        return True

    async def release(self) -> None:
        """Явно освободить блокировку до возврата физического соединения."""
        if not self.acquired:
            return
        try:
            await self.connection.scalar(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self.LOCK_ID}
            )
        finally:
            self.acquired = False


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
