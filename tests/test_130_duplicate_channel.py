"""Один канал дважды в одном источнике (найдено владельцем 2026-09-13).

Прогон источника «ТОП-вакансии» падал целиком:

    Ошибка извлечения: IntegrityError: duplicate key value violates unique
    constraint "monitor_channels_monitor_id_chat_id_key"
    DETAIL: Key (monitor_id, chat_id)=(6, 1093073202) already exists.

Ограничение сработало верно — в нём и был замысел: «дважды один канал в
одном источнике — двойной опрос и двойной счёт». Неверно было всё остальное.

**Как дубль попал в источник.** Канал добавляется ссылкой, а `chat_id`
появляется только при первом опросе. Проверка на повтор сравнивала
`chat_target` БУКВАЛЬНО, поэтому `@forproducts`, `t.me/forproducts`,
`https://t.me/forproducts/` и `FORPRODUCTS` — четыре разные строки и четыре
разрешённых к добавлению «разных» канала. Telethon разрешает их в один и тот
же чат.

**Чем это кончалось.** Ограничение стреляет не при добавлении, а при опросе,
в момент записи разрешённого `chat_id`. Исключение уходило выше `poll_source`,
и терялся **весь прогон источника** — включая каналы, которые разобрались
нормально. Один дубль обездвиживал источник из пяти каналов, причём каждый
раз: опрос повторялся по расписанию и падал снова.

**Что видел владелец.** Дамп SQL с параметрами вместо объяснения. Из такого
текста нельзя узнать ни какой канал лишний, ни что делать.

Лечится с трёх сторон, и ни одной из них по отдельности не хватает:

1. **Вход.** Повтор ловится по нормализованной цели, а не по строке.
2. **Прогон.** Столкновение перестаёт быть исключением: канал объявляется
   неразобранным с внятной причиной, остальные разбираются как обычно.
   Нормализация входа этого не заменяет — два РАЗНЫХ адреса (публичное имя и
   приглашение в тот же чат, переименованный канал) законно сходятся в один
   `chat_id`, и узнать об этом можно только после разрешения.
3. **Данные, которые уже лежат.** У владельца дубль в базе прямо сейчас;
   правило на входе его не уберёт, и источник обязан работать, пока дубль не
   удалят руками.
"""

import pytest
from sqlalchemy import select
from test_58_worker_body import FakeTelegram, _post, _worker
from test_112_map_reduce import RecordingLLM, _source

from app.models import LogEntry, MonitorChannel
from app.services.channels import normalize_channel_target

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Нормализация: обе стороны
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "@forproducts",
        "forproducts",
        "t.me/forproducts",
        "https://t.me/forproducts",
        "https://t.me/forproducts/",
        "http://telegram.me/forproducts",
        "  @ForProducts  ",
        "https://t.me/forproducts/1093",
    ],
)
async def test_every_spelling_of_one_channel_collapses_to_one_key(written):
    assert normalize_channel_target(written) == "forproducts"


@pytest.mark.parametrize(
    "first,second",
    [
        ("@alpha", "@beta"),
        ("t.me/+AAAAbbbb", "t.me/+CCCCdddd"),
        ("-1001093073202", "-1001093073203"),
    ],
)
async def test_different_channels_stay_different(first, second):
    """Обратная сторона: свернуть лишнее — значит запретить живой канал."""
    assert normalize_channel_target(first) != normalize_channel_target(second)


async def test_invite_hash_keeps_its_case():
    """Приглашение — не имя: хеш регистрозависим, приводить его нельзя."""
    assert normalize_channel_target("https://t.me/+AbCdEf") == "+AbCdEf"
    assert normalize_channel_target("t.me/joinchat/AbCdEf") == "joinchat/AbCdEf"


# --------------------------------------------------------------------------
# Вход: повтор не добавляется
# --------------------------------------------------------------------------


async def _create_source(anon_client) -> str:
    response = await anon_client.post(
        "/api/sources", json={"title": "ТОП-вакансии", "interval_minutes": 60}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["public_id"]


async def test_the_same_channel_under_another_spelling_is_refused(
    anon_client, db, user
):
    from conftest import act_as

    await act_as(anon_client, db, user)
    source_id = await _create_source(anon_client)

    first = await anon_client.post(
        f"/api/sources/{source_id}/channels", json={"chat_target": "@forproducts"}
    )
    assert first.status_code in (200, 201), first.text

    second = await anon_client.post(
        f"/api/sources/{source_id}/channels",
        json={"chat_target": "https://t.me/forproducts"},
    )
    assert second.status_code == 409, (
        "тот же канал другой ссылкой добавился как новый: опрос пойдёт дважды, "
        f"а запись chat_id упрётся в ограничение уже на прогоне — {second.text}"
    )
    assert "уже есть" in second.json()["detail"]


async def test_a_genuinely_different_channel_is_still_accepted(anon_client, db, user):
    """Антивакуум: запрет, отвергающий всё, тоже «проходит» первый тест."""
    from conftest import act_as

    await act_as(anon_client, db, user)
    source_id = await _create_source(anon_client)
    for target in ("@alpha", "https://t.me/beta"):
        response = await anon_client.post(
            f"/api/sources/{source_id}/channels", json={"chat_target": target}
        )
        assert response.status_code in (200, 201), f"{target}: {response.text}"


# --------------------------------------------------------------------------
# Прогон: столкновение не роняет источник
# --------------------------------------------------------------------------


async def test_a_collision_does_not_lose_the_whole_run(db, user):
    """Два канала источника разрешаются в один чат — прогон обязан выжить.

    Двойник `FakeTelegram` отдаёт одну и ту же сущность на любую цель: это и
    есть столкновение, которое в бою даёт `IntegrityError` при записи
    `chat_id` второму каналу.
    """
    source = await _source(
        db,
        user,
        public_id="src-dup",
        channels=[
            ("@alpha", None, 5, "искать А"),
            ("@alpha-mirror", None, 5, "искать Б"),
        ],
    )
    worker = _worker(db, telegram=FakeTelegram(posts=[_post(11), _post(12)]))
    worker.llm = RecordingLLM()

    outcome = await worker.poll_source(db, source)
    assert outcome != "failed", "прогон источника потерян целиком из-за дубля"

    # первый канал получил chat_id, второй остался без него и не переписал чужой
    channels = list(
        await db.scalars(
            select(MonitorChannel)
            .where(MonitorChannel.monitor_id == source.id)
            .order_by(MonitorChannel.position)
        )
    )
    resolved = [c.chat_id for c in channels if c.chat_id]
    assert len(resolved) == 1, (
        f"один чат записан двум каналам источника: {[c.chat_id for c in channels]}"
    )


async def test_the_duplicate_is_named_in_the_journal_without_sql(db, user):
    """Владельцу нужен канал и действие, а не дамп запроса с параметрами."""
    source = await _source(
        db,
        user,
        public_id="src-dup2",
        channels=[("@alpha", None, 5, ""), ("@alpha-mirror", None, 5, "")],
    )
    worker = _worker(db, telegram=FakeTelegram(posts=[_post(11)]))
    worker.llm = RecordingLLM()
    await worker.poll_source(db, source)

    entries = list(
        await db.scalars(select(LogEntry).where(LogEntry.user_id == user.id))
    )
    text = "\n".join(e.details or "" for e in entries)
    assert "IntegrityError" not in text and "UPDATE monitor_channels" not in text, (
        f"в журнале дамп SQL вместо объяснения: {text}"
    )
    assert "дубл" in text.lower() or "уже есть" in text.lower(), (
        f"журнал не называет причину — владельцу нечего чинить: {text}"
    )


# --------------------------------------------------------------------------
# Текст любой ошибки БД: объяснение, а не дамп запроса
# --------------------------------------------------------------------------


async def test_a_database_error_is_trimmed_to_its_reason():
    """SQLAlchemy кладёт в текст исключения ЗАПРОС И ПАРАМЕТРЫ.

    Владелец увидел четыре строки SQL с подстановками вместо причины.
    Читать это нельзя, а в журнале оно вдобавок соседствует с тенантными
    данными: параметры — это содержимое строк таблицы.

    Проверка на настоящем исключении SQLAlchemy, а не на строке-подделке:
    формат текста задаёт библиотека, и меняться он может без нас.
    """
    from sqlalchemy.exc import IntegrityError

    from app.worker import _reason

    exc = IntegrityError(
        "UPDATE monitor_channels SET chat_title=$1 WHERE id = $2",
        ("Job for Products", 15),
        Exception("duplicate key value violates unique constraint"),
    )
    reason = _reason(exc)
    assert "duplicate key" in reason, f"причина потеряна: {reason}"
    assert "[SQL:" not in reason and "[parameters:" not in reason, (
        f"в журнал уехал запрос с параметрами: {reason}"
    )
    assert "\n" not in reason, f"многострочная запись в журнале: {reason}"


async def test_trimming_does_not_swallow_an_ordinary_error():
    """Антивакуум: обрезка, оставляющая пустоту, «проходит» тест выше."""
    from app.worker import _reason

    assert _reason(ValueError("канал не найден")) == "ValueError: канал не найден"
    assert _reason(TimeoutError()) == "TimeoutError"


# --------------------------------------------------------------------------
# Тот же класс отказа шире одного дубля
# --------------------------------------------------------------------------


async def test_a_failure_while_saving_one_channel_keeps_the_others(
    db, user, monkeypatch
):
    """«Один канал не роняет источник» — правило, а не частный случай.

    В `poll_source` оно применялось только к ВЫБОРКЕ: неудача Telegram
    ловилась, канал объявлялся неразобранным, прогон шёл дальше. Всё, что
    после выборки — запись разрешённого канала, дедупликация, два коммита —
    оставалось незащищённым, и любая ошибка там уносила ВЕСЬ источник вместе
    с каналами, которые уже разобрались.

    Дубль (см. выше) был одним из способов туда попасть, и он закрыт
    отдельно. Но способ не единственный: гонка в дедупликации, обрыв
    соединения посреди цикла, любой отказ СУБД дают то же самое. Поэтому
    проверяется класс, а не конкретная причина: здесь падает сохранение
    первого канала, и второй обязан дойти до модели.
    """
    from test_112_map_reduce import ChannelTelegram

    import app.worker as worker_module

    source = await _source(
        db,
        user,
        public_id="src-partial",
        channels=[("@alpha", -1001, 5, "искать А"), ("@beta", -1002, 5, "искать Б")],
    )
    telegram = ChannelTelegram(
        {-1001: ("@alpha", [_post(11)]), -1002: ("@beta", [_post(21)])}
    )
    worker = _worker(db, telegram=telegram)
    llm = RecordingLLM()
    worker.llm = llm

    real_filter_new = worker_module.filter_new

    async def flaky_filter_new(session, user_id, chat_id, messages, **kwargs):
        if chat_id == -1001:
            raise RuntimeError("база моргнула на первом канале")
        return await real_filter_new(session, user_id, chat_id, messages, **kwargs)

    monkeypatch.setattr(worker_module, "filter_new", flaky_filter_new)

    try:
        outcome = await worker.poll_source(db, source)
    except Exception as exc:  # noqa: BLE001 — это и есть проверяемый дефект
        pytest.fail(
            "отказ на одном канале вынес весь прогон источника: "
            f"{type(exc).__name__}: {exc}"
        )

    assert outcome != "failed", "прогон объявлен неудачным целиком"
    prompts = [call["prompt"] for call in llm.calls]
    assert any("искать Б" in prompt for prompt in prompts), (
        f"второй канал потерян вместе с первым: разборов {len(llm.calls)}"
    )


async def test_the_saving_guard_does_not_swallow_a_healthy_run(db, user):
    """Антивакуум: обработчик, глотающий всё, «проходит» тест выше."""
    from test_112_map_reduce import ChannelTelegram

    source = await _source(
        db,
        user,
        public_id="src-ok",
        channels=[("@alpha", -1001, 5, "искать А"), ("@beta", -1002, 5, "искать Б")],
    )
    telegram = ChannelTelegram(
        {-1001: ("@alpha", [_post(11)]), -1002: ("@beta", [_post(21)])}
    )
    worker = _worker(db, telegram=telegram)
    llm = RecordingLLM()
    worker.llm = llm

    await worker.poll_source(db, source)
    prompts = [call["prompt"] for call in llm.calls]
    assert sum("искать" in p for p in prompts) >= 2, (
        f"здоровый прогон потерял каналы: {prompts}"
    )
