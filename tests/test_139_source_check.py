"""Проверка источников: видно, что канал встал и из него можно читать.

**Заказ владельца после разбора «VASILE LUNGO | INSIDER».** Тот канал не
разрешался **ни разу с момента добавления**, и узнать об этом было неоткуда:
источник просто не давал находок. Между «в канале сегодня пусто» и «канал не
открывался никогда» интерфейс не различал ничего.

Проверка отвечает на один вопрос по каждому каналу: **сможет ли прогон
прочитать отсюда сообщения.** Поэтому она идёт ТЕМ ЖЕ путём, что и прогон —
через `_resolve_channel`, со всеми его запасными ходами и прогревом кэша.
Проверка, которая разрешает канал по-своему, проверяет себя, а не прогон; это
та же ошибка, что тест, сеющий данные, которых писатель не пишет (11.9).

Проверяются пять состояний, и все пять названы словами, а не кодом:

- **ok** — разрешился, история читается, текстовые посты есть;
- **no_posts** — читается, но текста нет: монитору нечего извлекать;
- **not_found** — не разрешился; ровно случай владельца;
- **no_access** — разрешился, но история недоступна (не участник);
- **duplicate** — два адреса источника сошлись в один чат (живая проблема
  «ТОП-вакансий»: канал опрашивался бы дважды и считался дважды).

И отдельно — **вид** того, что нашлось: канал, супергруппа, группа, бот или
личный чат. Человек добавляет ссылку и вправе узнать, что она привела не туда,
куда он думал.

Проверка НЕ должна съедать дедупликацию: если проба пометит посты
прочитанными, ближайший прогон промолчит — и это выглядело бы как «проверил и
сломал».
"""

import pytest
from sqlalchemy import select
from test_58_worker_body import _worker
from test_112_map_reduce import _post, _source

from app.models import LogEntry, MonitorChannel, SentMessage

pytestmark = pytest.mark.asyncio


class Entity:
    def __init__(self, chat_id, title=None, username=None, **flags):
        self.id = chat_id
        self.title = title
        self.username = username
        for name, value in flags.items():
            setattr(self, name, value)


class CheckGateway:
    """Двойник шлюза: у каждой цели своя судьба.

    `resolves` — что отдаёт `resolve` (или исключение). `posts` — что отдаёт
    `fetch` по chat_id (или исключение). `needs_prime` — цели, которые
    оживают только после прогрева кэша: ровно поведение голого числового id.
    """

    def __init__(self, resolves, posts=None, needs_prime=()):
        self.resolves = resolves
        self.posts = posts or {}
        self.needs_prime = set(needs_prime)
        self.primed = 0
        self.tried: list = []
        self.fetched: list = []

    async def client_for(self, db, user_id):
        return object()

    async def prime(self, client):
        self.primed += 1

    async def resolve(self, client, target):
        self.tried.append(target)
        if target in self.needs_prime and not self.primed:
            raise ValueError("Could not find the input entity for PeerChannel")
        outcome = self.resolves.get(target)
        if outcome is None:
            raise ValueError(f"Cannot find any entity corresponding to {target!r}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fetch(self, client, entity, *, limit, offset_hours):
        self.fetched.append(entity.id)
        outcome = self.posts.get(entity.id, [])
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)[:limit]

    async def avatar(self, client, entity):
        return None


async def _channels(db, source) -> dict[str, MonitorChannel]:
    rows = await db.scalars(
        select(MonitorChannel).where(MonitorChannel.monitor_id == source.id)
    )
    return {row.chat_target: row for row in rows}


CHANNEL = Entity(-1001, title="AI Engineers Jobs", username="ai", broadcast=True)


# --------------------------------------------------------------------------
# Здоровый случай и то, что проверка попутно чинит
# --------------------------------------------------------------------------


async def test_a_healthy_channel_is_marked_ok_and_says_what_it_is(db, user):
    source = await _source(db, user, channels=[("@ai", None, 20, "искать")])
    telegram = CheckGateway({"@ai": CHANNEL}, posts={-1001: [_post(11)]})
    worker = _worker(db, telegram=telegram)

    await worker.check_source(db, source)

    row = (await _channels(db, source))["@ai"]
    assert row.check_status == "ok", f"здоровый канал помечен {row.check_status!r}"
    assert row.checked_at is not None, "время проверки не записано"
    assert "канал" in (row.check_detail or "").lower(), (
        f"не сказано, ЧТО нашлось по адресу: {row.check_detail!r}"
    )


async def test_the_check_teaches_the_row_its_chat(db, user):
    """Проверка не только смотрит: разрешённый чат запоминается.

    Иначе «всё встало» остаётся словами: ближайший прогон снова пойдёт
    разрешать адрес с нуля и снова может не дойти.
    """
    source = await _source(db, user, channels=[("@ai", None, 20, "искать")])
    worker = _worker(
        db,
        telegram=CheckGateway({"@ai": CHANNEL}, posts={-1001: [_post(11)]}),
    )

    await worker.check_source(db, source)

    row = (await _channels(db, source))["@ai"]
    assert row.chat_id == -1001, "разрешённый чат не сохранён"
    assert row.chat_title == "AI Engineers Jobs", "имя канала не сохранено"


async def test_a_direct_chat_is_named_as_a_direct_chat(db, user):
    """Владелец просил и про личные сообщения: вид обязан быть виден."""
    person = Entity(777, title=None, username="vasya", first_name="Вася")
    source = await _source(db, user, channels=[("@vasya", None, 20, "искать")])
    worker = _worker(
        db, telegram=CheckGateway({"@vasya": person}, posts={777: [_post(1)]})
    )

    await worker.check_source(db, source)

    detail = ((await _channels(db, source))["@vasya"].check_detail or "").lower()
    assert "личный" in detail, f"личный чат не назван личным: {detail!r}"


# --------------------------------------------------------------------------
# Пять состояний
# --------------------------------------------------------------------------


async def test_an_unresolvable_channel_is_not_found_and_says_why(db, user):
    """Случай владельца: канал не открывался никогда."""
    source = await _source(db, user, channels=[("-1004324020362", None, 20, "и")])
    worker = _worker(db, telegram=CheckGateway({}))

    await worker.check_source(db, source)

    row = (await _channels(db, source))["-1004324020362"]
    assert row.check_status == "not_found", (
        f"неразрешимый канал помечен {row.check_status!r}"
    )
    assert row.check_detail, "причина не названа — человека снова шлют в журнал"


async def test_a_channel_without_history_access_is_told_apart_from_a_missing_one(
    db, user
):
    """«Нашёлся, но не пускает» и «не нашёлся» лечатся по-разному."""
    source = await _source(db, user, channels=[("@closed", None, 20, "и")])
    closed = Entity(-2002, title="Закрытый", broadcast=True)
    worker = _worker(
        db,
        telegram=CheckGateway(
            {"@closed": closed}, posts={-2002: PermissionError("private channel")}
        ),
    )

    await worker.check_source(db, source)

    row = (await _channels(db, source))["@closed"]
    assert row.check_status == "no_access", (
        f"недоступная история помечена {row.check_status!r} — человек пойдёт "
        "искать опечатку в адресе вместо того, чтобы вступить в канал"
    )


async def test_a_channel_without_text_posts_is_not_called_healthy(db, user):
    """Читается, но извлекать нечего — это не «ok»."""
    source = await _source(db, user, channels=[("@pics", None, 20, "и")])
    worker = _worker(
        db,
        telegram=CheckGateway(
            {"@pics": Entity(-3003, title="Картинки", broadcast=True)},
            posts={-3003: []},
        ),
    )

    await worker.check_source(db, source)

    row = (await _channels(db, source))["@pics"]
    assert row.check_status == "no_posts", (
        f"канал без текстовых постов помечен {row.check_status!r}"
    )


async def test_two_addresses_that_lead_to_one_chat_are_reported(db, user):
    """Живая проблема «ТОП-вакансий»: дубль виден до прогона, а не после."""
    source = await _source(
        db,
        user,
        channels=[("@ai", None, 20, "и"), ("https://t.me/ai", None, 20, "и")],
    )
    worker = _worker(
        db,
        telegram=CheckGateway(
            {"@ai": CHANNEL, "https://t.me/ai": CHANNEL}, posts={-1001: [_post(11)]}
        ),
    )

    await worker.check_source(db, source)

    statuses = {t: row.check_status for t, row in (await _channels(db, source)).items()}
    assert "duplicate" in statuses.values(), (
        f"дубль не назван: {statuses}. Канал опрашивался бы дважды и считался дважды"
    )
    assert "ok" in statuses.values(), (
        f"обе строки объявлены дублями — оригинала не осталось: {statuses}"
    )


# --------------------------------------------------------------------------
# Проверка идёт тем же путём, что и прогон
# --------------------------------------------------------------------------


async def test_a_numeric_id_is_resolved_the_way_the_poll_resolves_it(db, user):
    """Голый числовой id оживает только после прогрева кэша (12.x, 0017-й день).

    Если проверка разрешает канал по-своему, она зелёная там, где прогон
    красный, — и это хуже, чем её отсутствие.
    """
    numeric = Entity(-1004324020362, title="VASILE LUNGO | INSIDER", broadcast=True)
    source = await _source(db, user, channels=[("-1004324020362", None, 20, "и")])
    telegram = CheckGateway(
        {-1004324020362: numeric},
        posts={-1004324020362: [_post(11)]},
        needs_prime=[-1004324020362],
    )
    worker = _worker(db, telegram=telegram)

    await worker.check_source(db, source)

    row = (await _channels(db, source))["-1004324020362"]
    assert row.check_status == "ok", (
        f"канал по числовому id не прошёл проверку ({row.check_status!r}), "
        "хотя прогон его разрешает — проверка проверяет себя, а не прогон"
    )
    assert telegram.primed == 1, "кэш не прогревался — путь не тот, что у прогона"


async def test_one_bad_channel_does_not_stop_the_check(db, user):
    """Ответ нужен по ВСЕМ каналам: иначе проверку гоняют по одному."""
    source = await _source(
        db,
        user,
        channels=[("@dead", None, 20, "и"), ("@ai", None, 20, "и")],
    )
    worker = _worker(
        db,
        telegram=CheckGateway({"@ai": CHANNEL}, posts={-1001: [_post(11)]}),
    )

    await worker.check_source(db, source)

    statuses = {t: row.check_status for t, row in (await _channels(db, source)).items()}
    assert statuses["@dead"] == "not_found", statuses
    assert statuses["@ai"] == "ok", f"проверка встала на первом отказе: {statuses}"


async def test_the_check_does_not_eat_the_deduplication(db, user):
    """Проба не помечает посты прочитанными.

    Иначе ближайший прогон промолчит, и это выглядит как «проверил и сломал».
    """
    source = await _source(db, user, channels=[("@ai", None, 20, "искать")])
    worker = _worker(
        db, telegram=CheckGateway({"@ai": CHANNEL}, posts={-1001: [_post(11)]})
    )

    await worker.check_source(db, source)

    seen = list(await db.scalars(select(SentMessage)))
    assert seen == [], (
        f"проверка записала {len(seen)} постов как прочитанные — ближайший "
        "прогон о них промолчит"
    )


async def test_the_result_is_written_to_the_journal(db, user):
    """Итог виден и в журнале: там человек ищет историю отказов."""
    source = await _source(db, user, channels=[("@dead", None, 20, "и")])
    worker = _worker(db, telegram=CheckGateway({}))

    await worker.check_source(db, source)

    events = [e.event_type for e in await db.scalars(select(LogEntry))]
    assert any("CHECK" in e for e in events), f"проверка не оставила следа: {events}"


async def test_a_source_without_a_telegram_account_says_so_once(db, user):
    """Аккаунт не подключён — это один ответ про источник, а не N про каналы."""

    class NoAccount(CheckGateway):
        async def client_for(self, db, user_id):
            return None

    source = await _source(
        db, user, channels=[("@ai", None, 20, "и"), ("@bi", None, 20, "и")]
    )
    worker = _worker(db, telegram=NoAccount({}))

    result = await worker.check_source(db, source)

    assert result.get("status") == "no_account", (
        f"молчаливый отказ вместо ответа: {result}"
    )
    rows = await _channels(db, source)
    assert all(row.check_status is None for row in rows.values()), (
        "каналы помечены отказом, хотя проверять их было нечем"
    )


# --------------------------------------------------------------------------
# Кабинет: кнопка, задача, карточка
# --------------------------------------------------------------------------


async def test_the_cabinet_can_ask_for_a_check(anon_client, db, user):
    from conftest import act_as

    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@ai", None, 20, "и")])

    response = await anon_client.post(f"/api/sources/{source.public_id}/check")

    assert response.status_code == 202, response.text
    from app.models import Job

    jobs = [j for j in await db.scalars(select(Job)) if j.kind == "check_source"]
    assert len(jobs) == 1, f"задача проверки не поставлена: {jobs}"


async def test_a_second_click_does_not_queue_a_second_check(anon_client, db, user):
    from conftest import act_as

    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@ai", None, 20, "и")])

    await anon_client.post(f"/api/sources/{source.public_id}/check")
    await anon_client.post(f"/api/sources/{source.public_id}/check")

    from app.models import Job

    jobs = [j for j in await db.scalars(select(Job)) if j.kind == "check_source"]
    assert len(jobs) == 1, (
        f"на каждый клик своя проверка ({len(jobs)}) — очередь забьётся "
        "одинаковыми обходами Telegram"
    )


async def test_the_check_of_another_cabinet_is_not_found(anon_client, db, user, user_b):
    """Изоляция — с положительным контролем.

    Первая версия этого теста зеленела на пустом месте: эндпоинта ещё не
    было, и 404 приходил всем подряд. Изоляционная проверка без контроля
    доказывает только то, что ручка не работает.
    """
    from conftest import act_as

    foreign = await _source(
        db, user_b, channels=[("@ai", None, 20, "и")], public_id="src-foreign"
    )
    mine = await _source(db, user, channels=[("@ai", None, 20, "и")])
    await act_as(anon_client, db, user)

    ok = await anon_client.post(f"/api/sources/{mine.public_id}/check")
    assert ok.status_code == 202, (
        f"своя проверка не работает ({ok.status_code}) — чужой 404 ничего не доказывает"
    )

    response = await anon_client.post(f"/api/sources/{foreign.public_id}/check")

    assert response.status_code == 404, (
        f"чужой источник ответил {response.status_code}: 403 подтверждает, "
        "что он существует"
    )


async def test_the_card_carries_the_verdict(anon_client, db, user):
    from conftest import act_as

    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@ai", None, 20, "и")])
    worker = _worker(
        db, telegram=CheckGateway({"@ai": CHANNEL}, posts={-1001: [_post(11)]})
    )
    await worker.check_source(db, source)

    card = (await anon_client.get("/api/sources")).json()["sources"][0]
    channel = card["channels"][0]
    assert channel.get("check_status") == "ok", (
        f"вердикт не доехал до карточки: {channel}"
    )
    assert channel.get("check_detail"), "карточка знает исход, но не причину"


def test_the_interface_shows_the_verdict_and_can_ask_for_it():
    """Стык API ↔ интерфейс: поле, которое сервер считает, а никто не читает,
    — та же тихая дыра, что увела аватарки источников на полгода (11.9).

    Смотрятся ВСЕ модули, а не один: первая версия теста искала поле в
    `sources.js`, а читает его `render.js` — и краснела на верном коде.
    Интерфейс здесь — вся папка, и требование «кто-то это читает» ровно
    такое же.
    """
    import pathlib

    js_dir = pathlib.Path(__file__).resolve().parents[1] / "static" / "js"
    js = "\n".join(p.read_text(encoding="utf-8") for p in sorted(js_dir.glob("*.js")))
    assert "check_status" in js, "интерфейс не читает вердикт проверки"
    assert "/check" in js, "интерфейс не умеет попросить проверку"


async def test_a_source_without_channels_says_so(db, user):
    """Пустой источник — законное состояние, и ответ на него не молчание."""
    source = await _source(db, user, channels=[])
    worker = _worker(db, telegram=CheckGateway({}))

    result = await worker.check_source(db, source)

    assert result.get("status") == "no_channels", f"пустой источник: {result}"


async def test_a_never_checked_channel_is_told_apart_from_a_healthy_one(db, user):
    """Страж пустоты: «не проверяли» обязано отличаться от «проверили, ok».

    Без этого пустое поле на карточке читается как зелёный вердикт — тот же
    класс, что «совпадений нет» против «не смогли посмотреть» (фаза 9).
    """
    source = await _source(db, user, channels=[("@ai", None, 20, "и")])

    row = (await _channels(db, source))["@ai"]
    assert row.check_status is None, (
        f"канал считается проверенным до первой проверки: {row.check_status!r}"
    )


async def test_status_tells_a_check_apart_from_a_run(anon_client, db, user):
    """Проверка и прогон стоят в одной очереди — и это разные состояния.

    Общий поиск «живой задачи» принял бы идущую проверку за идущий прогон:
    кнопка «Запустить» погасла бы сама собой, а нажатие на неё сервер тихо
    проглотил бы как повтор.
    """
    from conftest import act_as

    await act_as(anon_client, db, user)
    source = await _source(db, user, channels=[("@ai", None, 20, "и")])

    await anon_client.post(f"/api/sources/{source.public_id}/check")
    status = (await anon_client.get(f"/api/sources/{source.public_id}/status")).json()

    assert status.get("checking") is True, (
        f"идущая проверка не видна в состоянии источника: {status}"
    )
    assert status.get("running") is not True, (
        "проверка выдаёт себя за прогон — кнопка «Запустить» погаснет, и "
        "нажатие на неё сервер проглотит как повтор"
    )


# --------------------------------------------------------------------------
# Значок на канале — исполнением, а не разбором
# --------------------------------------------------------------------------


def _badge(payload: str) -> str:
    """Значок канала, посчитанный НАСТОЯЩИМ исполнением в node."""
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — значок не исполнить")
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            "import { checkBadge } from './static/js/render.js';\n"
            f"process.stdout.write(JSON.stringify(checkBadge({payload})));",
        ],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert result.returncode == 0, f"значок не посчитался: {result.stderr}"
    return result.stdout


def test_a_never_checked_channel_does_not_look_healthy():
    """Третье состояние: «не проверяли» ≠ «проверили, всё хорошо».

    Пустое поле, нарисованное как зелёная точка, — тот же класс ошибки, что
    «совпадений нет» против «не смогли посмотреть» (фаза 9).
    """
    import json

    badge = json.loads(_badge("{}"))
    healthy = json.loads(_badge('{check_status: "ok"}'))
    assert badge["cls"] != healthy["cls"], (
        f"непроверенный канал выглядит здоровым: {badge}"
    )
    assert badge["title"], "у значка нет пояснения — точка без смысла"


def test_a_failed_check_looks_different_from_a_warning():
    import json

    bad = json.loads(_badge('{check_status: "not_found", check_detail: "нет"}'))
    warn = json.loads(_badge('{check_status: "no_posts", check_detail: "пусто"}'))
    ok = json.loads(_badge('{check_status: "ok", check_detail: "читается"}'))
    tones = {bad["cls"], warn["cls"], ok["cls"]}
    assert len(tones) == 3, f"отказ, предупреждение и норма выглядят одинаково: {tones}"


def test_the_badge_shows_the_reason_the_server_gave():
    """Причина словами — смысл всей задачи: не отсылать человека в журнал."""
    import json

    badge = json.loads(
        _badge('{check_status: "no_access", check_detail: "аккаунт не состоит в нём"}')
    )
    assert "не состоит" in badge["title"], (
        f"причина с сервера потеряна по дороге: {badge}"
    )


def test_the_cabinet_can_check_every_source_at_once():
    """Вопрос владельца был про ВСЕ источники, а не про один.

    С девятью источниками «по кнопке на каждый» — это девять кликов и
    девять мест, где можно сбиться со счёта.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    index = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "js" / "sources.js").read_text(encoding="utf-8")
    assert 'id="checkAllSourcesBtn"' in index, (
        "нет кнопки «проверить все» — ответ на вопрос «все ли встали» "
        "собирается из отдельных кликов"
    )
    assert "checkAllSourcesBtn" in js, (
        "кнопка есть в разметке, но её никто не слушает: под CSP инлайновый "
        "обработчик не исполняется, и кнопка молча мертва (9.8)"
    )
