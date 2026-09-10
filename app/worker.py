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

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import TenantRepo, get_sessionmaker
from app.models import (
    FeedItem,
    Integration,
    Job,
    Monitor,
    MonitorChannel,
    SentMessage,
    TelegramAccount,
)
from app.security.crypto import key_fingerprint, validate_encryption_key
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
from app.services.llm import (
    MonthlyTokenBudgetExhausted,
    process_messages_batch_with_llm,
)
from app.services.ops import WORKER_NAME, record_heartbeat
from app.services.stopwords import parse_stop_words, split_by_stop_words
from app.services.tg_gateway import TelegramGateway
from app.services.tg_pool import TelegramClientPool, flood_guarded_call

logger = logging.getLogger(__name__)

# между тиками (порт background_monitor_worker: там 30 секунд)
TICK_INTERVAL = 30.0

# Потолок задач за тик. Без него один пользователь, поставивший сотню
# ручных запусков, откладывает расписание всех остальных на неопределённое
# время: очередь разбирается до дна раньше, чем цикл дойдёт до шага 3.
MAX_JOBS_PER_TICK = 20

# Тайм-ауты внешних вызовов (11.0). У HTTP-клиента OpenRouter тайм-аут свой
# (45 с в llm.py), у MTProto не было никакого: вызов Telethon, который не
# возвращается, останавливал тик целиком — а с ним очередь, расписание и
# очистку, то есть всех пользователей сразу.
TELEGRAM_TIMEOUT = 60.0

# Потолок на один разбор модели. У HTTP-клиента OpenRouter свой (45 с), но
# durable-режим режет длинный текст на несколько запросов подряд.
LLM_TIMEOUT = 120.0

# Столько прогонов подряд канал может не разбираться, прежде чем он будет
# отключён: мёртвый канал не должен вечно тратить время прогона.
MAX_CHANNEL_FAILURES = 5

# Столько раз батч источника пробует разобраться, прежде чем доставить то,
# что собрано. Без потолка один навсегда сломанный канал держал бы источник
# вечно; без повторов вообще — посты канала, упавшего на модели, пропали бы
# совсем: они уже зарезервированы дедупликацией.
MAX_BATCH_ATTEMPTS = 3

# Потолок на единицы работы в одном тике. Без него тик длится столько,
# сколько дают внешние сервисы.
TICK_BUDGET = 240.0


class StepTimeout(TimeoutError):
    """Внешний вызов не ответил за отведённое время.

    Подкласс TimeoutError, а не своя иерархия: перехватывающий код не
    обязан знать это имя, а `_reason` покажет и тип, и что именно молчало.
    """


async def _within(awaitable, seconds: float, what: str):
    """Выполнить внешний вызов с потолком по времени.

    Без потолка вызов Telethon, который не возвращается, останавливает тик
    целиком — очередь, расписание и очистку, то есть всех пользователей
    сразу (найдено на проде 9 сентября).
    """
    # asyncio.timeout, а не wait_for: у него есть expired(), и по нему видно,
    # ЧЕЙ это тайм-аут. wait_for отдаёт TimeoutError и когда истекло наше
    # время, и когда сам вызов упал по своему тайм-ауту, — подменять второе
    # на «тайм-аут 60 с» значит врать о причине (поймано тестом 9.11,
    # который подаёт asyncio.TimeoutError как отказ сети).
    guard = asyncio.timeout(seconds)
    try:
        async with guard:
            return await awaitable
    except TimeoutError:
        if guard.expired():
            raise StepTimeout(f"тайм-аут {seconds:g} с: {what}") from None
        raise


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


def _reason(exc: BaseException) -> str:
    """Тип и текст исключения одной строкой.

    Тип обязателен: самые частые сетевые отказы (`ConnectionError`,
    `asyncio.TimeoutError`) приходят без сообщения, и запись без типа
    сообщает ровно ничего — а логи процесса пользователю недоступны.
    """
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


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
            await record_heartbeat(
                db, WORKER_NAME, leader=True, fingerprint=key_fingerprint()
            )
            await requeue_hung_jobs(db)
            await self.run_jobs(db)
            await self.run_schedule(db)
            await self.run_cleanup(db)

    # ------------------------------------------------------------------
    # 1–2. Очередь
    # ------------------------------------------------------------------

    async def run_jobs(self, db, **senders) -> int:
        """Разобрать очередь. Падение задачи — failed и следующая: один
        сломанный канал не останавливает остальных пользователей."""
        done = 0
        deadline = asyncio.get_running_loop().time() + TICK_BUDGET
        for _ in range(MAX_JOBS_PER_TICK):
            if self._out_of_budget(deadline):
                logger.info(
                    "бюджет тика исчерпан, очередь разбирается дальше в следующем тике"
                )
                break
            job = await claim_next_job(db)
            if job is None:
                break
            # id и вид запоминаются ДО выполнения: rollback ниже обесценивает
            # (expire) ORM-объект, и любое обращение к его полю после отката
            # — это ленивая загрузка, то есть MissingGreenlet в async-сессии
            job_id, job_kind = job.id, job.kind
            try:
                await self._run_job(db, job, **senders)
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
                        stale.attempts = (stale.attempts or 0) + 1
                        stale.retry_after = _retry_deadline(exc)
                        stale.error = redact(_reason(exc))[:MAX_ERROR_CHARS]
                        await db.commit()
                    else:
                        await fail_job(
                            db, stale, error=redact(_reason(exc))[:MAX_ERROR_CHARS]
                        )
                logger.warning(
                    "задача %s (%s) упала: %s", job_id, job_kind, redact(_reason(exc))
                )
            else:
                await finish_job(db, job)
                done += 1
            await self._beat(db)
        return done

    async def _run_job(self, db, job, **senders) -> None:
        try:
            payload = json.loads(job.payload_json or "{}")
        except ValueError as exc:
            raise ValueError(f"повреждённый payload задачи: {exc}") from exc

        if job.kind == KIND_BATCH:
            if payload.get("batch", {}).get("channels"):
                result = await self._run_source_batch(
                    db, job.user_id, payload, job=job, **senders
                )
            else:
                # Батч, поставленный до Фазы 11: один канал, плоский список
                result = await self._dispatch(
                    db,
                    job.user_id,
                    payload["batch"],
                    channel_prompt=payload.get("prompt"),
                    **senders,
                )
            if result.get("retry"):
                # Отказ доставки не считается выполненной работой — иначе
                # разобранный батч тихо теряется вместе с его постами.
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
                raise LookupError("источник не найден у владельца задачи")
            # Имя другое: у ветки батча `result` — словарь диспетчера, а
            # здесь исход опроса строкой. Одно имя на два типа mypy ловит
            # справедливо: читающему это тоже мешало бы.
            outcome = await self.poll_source(db, monitor, **senders)
            if outcome == "flood_wait":
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

    async def _run_source_batch(
        self, db, user_id: int, payload: dict, job=None, deliver=True, **senders
    ) -> dict:
        """Разбор по каналам, затем сведение (конвейер 11.4).

        **Разборы идут последовательно, а не параллельно** — и это
        сознательное отступление от плана. `AsyncSession` не рассчитан на
        одновременные операции, а разбор ведёт учёт израсходованных
        токенов, то есть работает с той же сессией. Параллельность здесь
        дала бы гонку в базе ради экономии минут; каждый вызов и так
        ограничен тайм-аутом, а весь прогон — бюджетом тика.

        Пустой вердикт при непустом входе — самый вероятный тихий отказ
        схемы, поэтому он попадает в журнал отдельным событием: иначе
        кривой промпт канала неотличим от «совпадений нет».
        """
        batch = payload["batch"]
        groups = batch.get("channels") or []
        answer_prompt = payload.get("answer_prompt") or ""
        unparsed = list(batch.get("unparsed") or [])
        verdicts: list[dict] = []
        caller = senders.get("llm_caller")
        attempts = getattr(job, "attempts", 0) or 0

        async def _save_progress() -> None:
            """Вердикты копятся в payload задачи.

            Повтор после сбоя не переспрашивает уже разобранные каналы:
            модель — самая дорогая часть прогона, и платить за неё дважды
            из-за отказа доставки незачем (контракт 9.1, перенесённый на
            конвейер).
            """
            if job is None:
                return
            job.payload_json = json.dumps(payload, ensure_ascii=False)
            await db.commit()

        single = len(groups) == 1
        for group in groups:
            if group.get("verdict"):
                # уже разобран прошлой попыткой
                verdicts.append(group)
                continue
            prompt = group.get("extract_prompt") or ""
            if single and answer_prompt:
                # Один канал — сводить нечего: извлечение и оформление
                # уходят одним запросом. Частый случай не должен стоить вдвое.
                prompt = f"{prompt}\n\n{answer_prompt}".strip()

            async def _chunk_done(done, group=group):
                """Удачные куски длинного канала переживают повтор.

                Без этого отказ провайдера на втором запросе заставлял бы
                платить за первый заново — а длинный канал режется на
                несколько запросов (контракт 9.1).
                """
                group["completed"] = list(done)
                await _save_progress()

            try:
                verdict = await _within(
                    self.llm(
                        db,
                        user_id,
                        group["messages"],
                        custom_prompt=prompt,
                        caller=caller,
                        require_success=True,
                        completed=list(group.get("completed") or []),
                        checkpoint=_chunk_done,
                    ),
                    LLM_TIMEOUT,
                    f"разбор канала {group.get('chat_title')}",
                )
            except MonthlyTokenBudgetExhausted:
                # Исчерпан месячный бюджет — это про весь источник, а не про
                # канал: следующий упрётся в тот же потолок. Задача
                # откладывается целиком и вернётся в новом периоде,
                # сохранив уже разобранные каналы (контракт 4.5).
                raise
            except RuntimeError as exc:
                if "LLM enabled without API key" in str(exc):
                    # То же самое: ключа нет у источника, а не у канала
                    raise
                if attempts + 1 < MAX_BATCH_ATTEMPTS:
                    # Отказ модели восстановим, а посты канала уже
                    # зарезервированы дедупликацией: пометить канал
                    # неразобранным и пойти дальше — значит потерять их
                    # навсегда. Батч возвращается в очередь; разобранные
                    # каналы при повторе не переспрашиваются.
                    raise
                await db.rollback()
                unparsed.append(group.get("chat_title") or "канал")
                await add_log(
                    db,
                    user_id,
                    "AI_ERROR",
                    f"Разбор канала не удался: {redact(_reason(exc))}",
                    status="ERROR",
                    chat_id=group.get("chat_id"),
                    chat_title=group.get("chat_title"),
                )
                continue
            except Exception as exc:  # noqa: BLE001 — канал не роняет источник
                await db.rollback()
                unparsed.append(group.get("chat_title") or "канал")
                await add_log(
                    db,
                    user_id,
                    "AI_ERROR",
                    f"Разбор канала не удался: {redact(_reason(exc))}",
                    status="ERROR",
                    chat_id=group.get("chat_id"),
                    chat_title=group.get("chat_title"),
                )
                continue
            if verdict:
                group["verdict"] = verdict
                verdicts.append(group)
                await _save_progress()
            else:
                await add_log(
                    db,
                    user_id,
                    "AI_EMPTY",
                    f"Модель ничего не извлекла из {len(group['messages'])} постов",
                    status="SKIPPED",
                    chat_id=group.get("chat_id"),
                    chat_title=group.get("chat_title"),
                )
            await self._beat(db)

        analysis = ""
        if verdicts and single:
            analysis = verdicts[0]["verdict"]
        elif verdicts:
            # Сведение видит вердикты в порядке каналов источника — он же
            # задаёт порядок в сообщении.
            summary_input = [
                {
                    "id": index + 1,
                    "chat_title": item.get("chat_title"),
                    "text": item["verdict"],
                }
                for index, item in enumerate(verdicts)
            ]
            try:
                analysis = await _within(
                    self.llm(
                        db,
                        user_id,
                        summary_input,
                        custom_prompt=answer_prompt,
                        caller=caller,
                        require_success=True,
                    ),
                    LLM_TIMEOUT,
                    "сведение по каналам",
                )
            except Exception as exc:  # noqa: BLE001 — находки дороже оформления
                await db.rollback()
                await add_log(
                    db,
                    user_id,
                    "AI_ERROR",
                    f"Сведение не удалось: {redact(_reason(exc))}",
                    status="ERROR",
                    chat_title=batch.get("chat_title"),
                )
                # Сырые вердикты лучше потерянных находок
                analysis = "\n\n".join(
                    f"{item.get('chat_title')}: {item['verdict']}" for item in verdicts
                )

        if analysis and unparsed:
            # Строку пишет система, а не модель: модель может её
            # проигнорировать или переврать, а это единственное место, где
            # видно, что картина неполная.
            analysis = f"{analysis}\n\n⚠️ Не разобраны: {', '.join(unparsed)}"

        messages = [m for group in groups for m in group["messages"]]
        if not deliver:
            # Переразбор обновляет карточку и НИЧЕГО не отправляет: нажатие
            # «обновить» не должно рассылать второе сообщение об одном и
            # том же батче (контракт 9.18).
            return {"status": "reanalyzed", "analysis": analysis}
        return await self._dispatch(
            db,
            user_id,
            {
                "job_id": batch.get("job_id"),
                "chat_title": batch.get("chat_title"),
                "chat_id": None,
                "chat_username": "",
                "messages_count": len(messages),
                "messages": messages,
            },
            analysis=analysis,
            **senders,
        )

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
        # Переразбор идёт ТЕМ ЖЕ конвейером, что и плановый (11.8): посты
        # группируются по каналам, у каждого свой промпт извлечения, затем
        # сведение промптом источника. Иначе переразбор отвечает по другим
        # правилам, и выглядит это как «модель стала хуже отвечать».
        payload = await self._batch_from_feed(db, user_id, item, messages)
        result = await self._run_source_batch(db, user_id, payload, deliver=False)
        analysis = result.get("analysis") or ""
        if analysis:
            item.ai_analysis = analysis
            await db.commit()

    async def _batch_from_feed(
        self, db, user_id: int, item: FeedItem, messages: list[dict]
    ) -> dict:
        """Собрать батч конвейера из сохранённых постов карточки ленты."""
        source = None
        if item.monitor_id is not None:
            source = await db.get(Monitor, item.monitor_id)
        channels = (
            list(
                await db.scalars(
                    select(MonitorChannel).where(MonitorChannel.monitor_id == source.id)
                )
            )
            if source is not None
            else []
        )
        prompts = {c.chat_id: c.extract_prompt or "" for c in channels}
        groups: dict[int, dict] = {}
        for message in messages:
            chat_id = message.get("chat_id") or item.chat_id or 0
            group = groups.setdefault(
                chat_id,
                {
                    "chat_id": chat_id,
                    "chat_title": message.get("chat_title") or item.chat_title,
                    "extract_prompt": prompts.get(chat_id, ""),
                    "filtered_count": 0,
                    "messages": [],
                },
            )
            group["messages"].append(message)
        return {
            "batch": {
                "job_id": item.job_id,
                "chat_title": source.title if source is not None else item.chat_title,
                "messages_count": len(messages),
                "channels": list(groups.values()),
                "unparsed": [],
            },
            "answer_prompt": source.answer_prompt if source is not None else "",
        }

    # ------------------------------------------------------------------
    # 3. Расписание
    # ------------------------------------------------------------------

    def _is_due(self, monitor: Monitor, now: datetime.datetime) -> bool:
        # Часы источника, а не канала: `last_checked` уехал в
        # monitor_channels, и по нему расписание либо не сработало бы
        # никогда, либо срабатывало каждый тик.
        last = _aware(monitor.last_run_at)
        if last is None:  # только что добавленный канал — опрос сразу
            return True
        return now >= last + datetime.timedelta(minutes=monitor.interval_minutes)

    async def _beat(self, db) -> None:
        """Отметиться о ПРОДВИЖЕНИИ, а не о запуске процесса.

        Задача 10.2 ставила отметку первой в тике: «зависший внутри тика
        обязан выглядеть мёртвым». Правило верное, но прогон источника из
        пяти каналов законно идёт минутами — при отметке раз в тик он
        неотличим от зависания (порог устаревания 180 с).

        Отметка между единицами работы решает обе задачи сразу: идущий
        прогон отмечается и виден живым, а зависший вызов до следующей
        отметки дойти не даёт. Отдельной задачей по таймеру этого делать
        НЕЛЬЗЯ — она отмечалась бы и при намертво вставшем тике.
        """
        try:
            await record_heartbeat(
                db, WORKER_NAME, leader=True, fingerprint=key_fingerprint()
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — отметка не стоит падения тика
            logger.debug("отметка не записана: %s", redact(_reason(exc)))

    def _out_of_budget(self, deadline: float) -> bool:
        return asyncio.get_running_loop().time() >= deadline

    async def run_schedule(self, db, **senders) -> int:
        now = _utcnow()
        deadline = asyncio.get_running_loop().time() + TICK_BUDGET
        monitors = list(
            await db.scalars(select(Monitor.id).where(Monitor.is_active.is_(True)))
        )
        polled = 0
        for monitor_id in monitors:
            if self._out_of_budget(deadline):
                # Бюджет исчерпан — остальные подождут следующего тика.
                # Иначе тик длится столько, сколько дают внешние сервисы,
                # а очередь и очистка стоят всё это время.
                logger.info("бюджет тика исчерпан, опрос отложен")
                break
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
            chat_id, chat_title = None, monitor.title
            public_id = monitor.public_id
            try:
                outcome = await self.poll_source(db, monitor, **senders)
                # Прерванный или несостоявшийся опрос не считается
                # выполненным: счётчик — это диагностика, и врать он не
                # должен даже в мелочах.
                if outcome not in ("failed", "flood_wait", "no_account", "no_channels"):
                    polled += 1
            except Exception as exc:  # noqa: BLE001 — один канал не роняет цикл
                await db.rollback()
                await add_log(
                    db,
                    owner_id,
                    "POLL_ERROR",
                    # Тип — всегда: самые частые сетевые отказы
                    # (ConnectionError, TimeoutError) приходят БЕЗ текста, и
                    # без типа запись выглядела как «Ошибка извлечения:» и
                    # ничего не сообщала (найдено на живом канале 7 сентября)
                    f"Ошибка извлечения: {_reason(exc)}",
                    status="ERROR",
                    chat_id=chat_id,
                    chat_title=chat_title,
                )
                logger.warning(
                    "источник %s тенанта %s: опрос упал — %s",
                    public_id,
                    owner_id,
                    # Причина — тип и текст, но НЕ трейсбек: он несёт
                    # окружение вызова, где встречаются учётные данные.
                    # (Затирание секретов стоит на канале логов — 9.4, —
                    # но кормить его лишним незачем.)
                    _reason(exc),
                )
            # Единица работы закончена — отмечаемся. Долгий, но идущий
            # обход каналов обязан выглядеть живым (11.0).
            await self._beat(db)
        return polled

    async def _resolve_channel(self, client, channel: dict):
        """Сначала по `chat_id`, потом по ссылке — и обновить оба.

        Порядок неочевиден и важен. По ссылке первым нельзя: у
        переименованного канала username меняется, а рабочий `chat_id`
        остался бы неиспользованным. По `chat_id` первым — но с запасным
        путём: перенесённый из старой базы идентификатор не находится в
        кэше новой сессии Telethon (это и был отказ 9.15).
        """
        targets: list = []
        if channel.get("chat_id"):
            targets.append(channel["chat_id"])
        if channel.get("chat_target"):
            targets.append(channel["chat_target"])
        last: Exception | None = None
        for target in targets:
            try:
                return await _within(
                    self.telegram.resolve(client, target),
                    TELEGRAM_TIMEOUT,
                    f"разрешение канала {target}",
                )
            except Exception as exc:  # noqa: BLE001 — пробуем следующий способ
                last = exc
        raise last or LookupError("канал нечем разрешить")

    async def poll_source(self, db, source: Monitor, **senders) -> str:
        """Обойти каналы источника и поставить общий разбор в очередь.

        Выборка идёт здесь, разбор — в задаче: сеть и модель разделены,
        поэтому сбой доставки не заставляет заново читать Telegram.
        """
        user_id = source.user_id
        channels = list(
            await db.scalars(
                select(MonitorChannel)
                .where(
                    MonitorChannel.monitor_id == source.id,
                    MonitorChannel.is_active.is_(True),
                )
                .order_by(MonitorChannel.position, MonitorChannel.id)
            )
        )
        if not channels:
            await add_log(
                db,
                user_id,
                "SCHEDULER_POLL",
                f"У источника «{source.title}» нет активных каналов.",
                status="SKIPPED",
                chat_title=source.title,
            )
            return "no_channels"

        account = (
            await db.scalars(TenantRepo(db, user_id).query(TelegramAccount))
        ).first()
        account_retry = _aware(account.retry_after) if account is not None else None
        if account_retry is not None and _utcnow() < account_retry:
            return "flood_wait"

        async def _flood(exc) -> None:  # noqa: D401
            """FloodWait — про весь аккаунт, а не про канал.

            Следующий канал упрётся в тот же лимит, поэтому обход
            прекращается целиком, а не переходит к соседу.
            """
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
                f"опрос «{source_title}» пропущен",
                status="SKIPPED",
                chat_title=source_title,
            )

        # Источник — тоже ORM-объект той же сессии, и rollback обесценивает
        # и его: `source.id` в середине обхода стал бы ленивой загрузкой.
        source_id = source.id
        source_title = source.title
        source_stop_words = source.stop_words or ""
        source_answer_prompt = source.answer_prompt or ""
        source_public_id = source.public_id

        # Поля снимаются ДО обхода, все сразу. Rollback в ветке ошибки
        # обесценивает НЕ только текущий объект, а всю сессию: следующая
        # итерация обращалась бы к полю уже обесцененной строки, то есть
        # к ленивой загрузке — MissingGreenlet в async-сессии. Тот же
        # корень, что у задач очереди и расписания выше, но подножка здесь
        # тоньше: цикл выглядит независимым, а сессия у него общая.
        plan = [
            {
                "id": c.id,
                "chat_target": c.chat_target,
                "chat_id": c.chat_id,
                "name": c.chat_title or c.chat_target,
                "limit_count": c.limit_count,
                "offset_hours": c.offset_hours,
                "extract_prompt": c.extract_prompt or "",
            }
            for c in channels
        ]

        # Клиент берётся ПОД защитой от FloodWait: подключение — такой же
        # запрос к Telegram, и в старом пути оно тоже было внутри guard'а.
        # Снаружи FloodWaitError улетал бы в расписание, retry_after не
        # ставился, и следующий источник того же аккаунта шёл в тот же лимит.
        connected = await flood_guarded_call(
            lambda: self.telegram.client_for(db, user_id), on_flood_wait=_flood
        )
        if connected is None:
            return "flood_wait"
        client = connected

        groups: list[dict] = []
        unparsed: list[str] = []
        entities: dict[int, object] = {}
        filtered_total = 0
        for spec in plan:
            channel_id = spec["id"]
            name = spec["name"]

            async def _work(spec=spec):
                entity = await self._resolve_channel(client, spec)
                messages = await _within(
                    self.telegram.fetch(
                        client,
                        entity,
                        limit=spec["limit_count"],
                        offset_hours=spec["offset_hours"],
                    ),
                    TELEGRAM_TIMEOUT,
                    f"выборка сообщений {spec['chat_target']}",
                )
                return entity, messages

            try:
                # FloodWaitError не ретраится и не спит inline: ожидание на
                # тысячи секунд повесило бы воркера, то есть всех тенантов
                result = await flood_guarded_call(_work, on_flood_wait=_flood)
                if result is None:
                    return "flood_wait"
                entity, messages = result
            except Exception as exc:  # noqa: BLE001 — один канал не роняет источник
                await db.rollback()
                stale = await db.get(MonitorChannel, channel_id)
                if stale is not None:
                    stale.fail_streak += 1
                    if stale.fail_streak >= MAX_CHANNEL_FAILURES:
                        # Мёртвый канал не должен вечно тратить время прогона
                        stale.is_active = False
                    await db.commit()
                unparsed.append(name)
                await add_log(
                    db,
                    user_id,
                    "POLL_ERROR",
                    f"Ошибка извлечения: {_reason(exc)}",
                    status="ERROR",
                    chat_title=name,
                )
                logger.warning(
                    "канал %s источника %s тенанта %s: опрос упал — %s",
                    name,
                    source_public_id,
                    user_id,
                    # Причина — тип и текст, но НЕ трейсбек: он несёт
                    # окружение вызова, где встречаются учётные данные.
                    _reason(exc),
                )
                continue

            channel = await db.get(MonitorChannel, channel_id)
            if channel is None:  # источник правили во время прогона
                continue
            channel.chat_id = int(getattr(entity, "id", 0) or channel.chat_id or 0)
            channel.chat_title = getattr(entity, "title", None) or channel.chat_title
            channel.chat_username = (
                getattr(entity, "username", None) or channel.chat_username
            )
            channel.last_checked = _utcnow()
            channel.fail_streak = 0
            entities[channel.chat_id] = entity
            chat_id = channel.chat_id
            chat_title = channel.chat_title
            chat_username = channel.chat_username or ""
            await db.commit()

            fresh = await filter_new(
                db,
                user_id,
                chat_id,
                messages,
                monitor_id=source_id,
                commit=False,
                processed=False,
            )
            fresh, filtered = split_by_stop_words(
                fresh, parse_stop_words(source_stop_words)
            )
            if filtered:
                await db.execute(
                    update(SentMessage)
                    .where(
                        SentMessage.monitor_id == source_id,
                        SentMessage.chat_id == chat_id,
                        SentMessage.message_id.in_([m["id"] for m in filtered]),
                    )
                    .values(processed=True)
                )
            await db.commit()
            filtered_total += len(filtered)
            if not fresh:
                continue
            for message in fresh:
                # у поста своя принадлежность: в сводке из пяти каналов без
                # неё нельзя ни сослаться, ни отметить обработанным
                message["chat_id"] = chat_id
                message["chat_title"] = chat_title
            groups.append(
                {
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "chat_username": chat_username,
                    "extract_prompt": spec["extract_prompt"],
                    "filtered_count": len(filtered),
                    "messages": fresh,
                }
            )
            await self._beat(db)

        source = await db.get(Monitor, source_id) or source
        source.last_run_at = _utcnow()
        if account is not None:
            account.retry_after = None
        await db.commit()

        if not groups:
            if unparsed and not filtered_total:
                # Ни один канал не удалось разобрать — это НЕ «новых нет».
                # Прогон, посчитанный успешным, скрыл бы отказ: снаружи
                # тишина выглядела бы нормой (контракт 11.0).
                return "failed"
            # «Всё отсеяно» и «новых нет» — разные исходы: слишком широкое
            # стоп-слово иначе выглядит как замолчавший источник (11.3).
            if filtered_total:
                await add_log(
                    db,
                    user_id,
                    "SCHEDULER_POLL",
                    f"Опрос «{source_title}» завершён: {filtered_total} постов "
                    "отсеяно стоп-словами, до анализа не дошло ничего.",
                    status="SKIPPED_STOPWORDS",
                    chat_title=source_title,
                )
                return "filtered_out"
            await add_log(
                db,
                user_id,
                "SCHEDULER_POLL",
                f"Опрос «{source_title}» завершён: новых постов нет.",
                status="SKIPPED_DEDUP",
                chat_title=source_title,
            )
            return "no_new"

        total = sum(len(g["messages"]) for g in groups)
        job = Job(
            user_id=user_id,
            kind=KIND_BATCH,
            payload_json=json.dumps(
                {
                    "batch": {
                        "job_id": str(uuid.uuid4()),
                        "source_public_id": source_public_id,
                        "chat_title": source_title,
                        "messages_count": total,
                        "channels": groups,
                        "unparsed": unparsed,
                    },
                    "answer_prompt": source_answer_prompt,
                },
                ensure_ascii=False,
            ),
            status="running",
            started_at=_utcnow(),
        )
        db.add(job)
        await db.commit()
        job_id = job.id
        try:
            # Аватарка косметическая и не должна мешать разбору батча
            for chat_id, entity in entities.items():
                try:
                    avatar = await _within(
                        self.telegram.avatar(client, entity),
                        TELEGRAM_TIMEOUT,
                        "загрузка аватарки канала",
                    )
                    if avatar:
                        await store_avatar(db, chat_id, avatar)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("avatar unavailable: %s", redact(_reason(exc)))
                    await db.rollback()
            job = await db.get(Job, job_id)
            await self._run_job(db, job, **senders)
            await finish_job(db, job)
        except Exception as exc:  # noqa: BLE001 — батч ждёт повтора, а не теряется
            await db.rollback()
            job = await db.get(Job, job_id)
            if job is not None:
                job.status = "pending"
                job.started_at = None
                job.attempts = (job.attempts or 0) + 1
                job.error = redact(_reason(exc))[:MAX_ERROR_CHARS]
                job.retry_after = _retry_deadline(exc)
                await db.commit()
            logger.warning("batch %s awaiting retry: %s", job_id, redact(_reason(exc)))
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
                    logger.warning("тик воркера упал: %s", redact(_reason(exc)))
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
