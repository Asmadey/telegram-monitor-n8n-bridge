"""Находки собирает код, а не вторая модель (задача 13.9).

Предложение владельца: раз каждый канал и так отвечает JSON, пусть код
объединяет эти JSON, а не вторая модель. Повод — два отказа за один день с
одинаковым симптомом (JSON вместо сообщения) и разными причинами: склейка
промптов (`test_142`) и тайм-аут сведения (`test_145`). Общий корень — итог
СОЧИНЯЕТ модель, поэтому его нельзя ни предсказать, ни удешевить, ни
отрисовать заново без нового вызова.

Проект этот принцип уже принял на шаг ниже. `telegram_markup` в первых
строках: «Промпт — не гарантия… Формат ответа — обязанность доставки». Там про
разметку, здесь — про сборку.

**Схема — один объект Pydantic на три применения** (решение владельца):
провайдеру в `response_format`, себе для валидации ответа, промпту канала —
текстом контракта. Разойтись они не могут, потому что источник один.

Границы, которые тут закрепляются:

1. Корень схемы — ОБЪЕКТ, а не массив: строгий режим структурного вывода
   массив верхним уровнем не принимает.
2. Все поля обязательные и строковые, пустая строка = «не указано». Иначе
   строгий режим требует возни с `Optional`, а `anyOf` в схеме ломается у
   половины провайдеров.
3. Не всякая модель умеет `json_schema`. Запрос, отвергнутый из-за
   `response_format`, повторяется без него ОДИН раз — иначе смена модели в
   настройках молча убила бы все источники.
4. Кривой объект выбрасывается ПОШТУЧНО. Один сломанный не топит девятнадцать
   целых, и число выброшенных идёт в журнал: тихая потеря хуже отказа.
"""

import json

import pytest
from sqlalchemy import select
from test_58_worker_body import RecordingDispatcher, _worker
from test_112_map_reduce import THREE, ChannelTelegram, RecordingLLM, _run, _source

from app.models import FeedItem, LogEntry
from app.services.findings import (
    CHANNEL_CONTRACT,
    Findings,
    merge_findings,
    parse_findings,
    render_findings,
    response_schema,
)

pytestmark = pytest.mark.asyncio


def _finding(**over) -> dict:
    row = {
        "title": "Менеджер по продажам",
        "company": "Компания",
        "salary": "от 100 000 ₽",
        "format": "офис",
        "location": "Москва",
        "description": "Активные продажи",
        "category": "sales",
        "post_url": "https://t.me/alpha/1",
    }
    row.update(over)
    return row


def _answer(*items) -> str:
    return json.dumps(list(items), ensure_ascii=False)


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------


def test_the_schema_root_is_an_object_not_an_array():
    """Строгий режим структурного вывода массив верхним уровнем не принимает."""
    schema = Findings.model_json_schema()
    assert schema.get("type") == "object", (
        f"корень схемы не объект: {schema.get('type')}"
    )
    assert "findings" in schema.get("properties", {}), schema.get("properties")


def test_the_schema_is_strict_enough_for_structured_output():
    """Строгий режим требует: все ключи обязательны, чужих полей нет.

    Ошибка первой версии теста, исправлена в тесте: строгость проверялась на
    `model_json_schema()`, то есть на НАШЕЙ валидации. Но валидация обязана
    быть терпимой (`extra="ignore"`): модель дописывает свои поля, и чужое
    поле — не повод выбросить находку. Строгой должна быть схема, которую мы
    ОТДАЁМ провайдеру, — её и проверяем.
    """
    schema = response_schema()
    finding = schema["$defs"]["Finding"]
    assert schema.get("additionalProperties") is False, "корень схемы не строгий"
    assert finding.get("additionalProperties") is False, (
        "схема допускает лишние поля — строгий режим такую не примет"
    )
    assert set(finding["required"]) == set(finding["properties"]), (
        f"не все поля обязательны: {set(finding['properties']) - set(finding['required'])}"
    )
    for name, spec in finding["properties"].items():
        assert spec.get("type") == "string", (
            f"поле {name} не строка ({spec}) — необязательность в строгом режиме "
            "даёт anyOf, который половина провайдеров не разбирает"
        )


def test_the_prompt_contract_is_generated_from_the_schema():
    """Контракт в промпте и схема разойтись не могут: источник один."""
    for name in Findings.model_json_schema()["$defs"]["Finding"]["properties"]:
        assert name in CHANNEL_CONTRACT, (
            f"поле {name} есть в схеме, но не названо в контракте промпта"
        )


# --------------------------------------------------------------------------
# Разбор ответа
# --------------------------------------------------------------------------


def test_a_fenced_answer_is_parsed():
    items, leftover = parse_findings(f"```json\n{_answer(_finding())}\n```")
    assert len(items) == 1 and not leftover, (items, leftover)


def test_prose_around_the_array_does_not_stop_the_parser():
    text = f"Вот что нашлось:\n{_answer(_finding())}\nБольше ничего."
    items, _ = parse_findings(text)
    assert len(items) == 1, f"массив в окружении прозы не найден: {items}"


def test_a_truncated_answer_returns_the_text_instead_of_raising():
    """Обрыв по длине — самый частый способ сломать JSON."""
    broken = _answer(_finding())[:-12]
    items, leftover = parse_findings(broken)
    assert items == [] and leftover, "оборванный ответ потерян молча"


def test_a_finding_without_a_link_is_dropped_and_the_rest_survive():
    """Правило владельца: без ссылки находку нельзя сделать кликабельной."""
    items, _ = parse_findings(
        _answer(
            _finding(post_url=""), _finding(title="Второй", post_url="https://t.me/a/2")
        )
    )
    assert [i["title"] for i in items] == ["Второй"], items


def test_one_broken_object_does_not_sink_the_others():
    raw = '[{"title": "Первый", "post_url": "https://t.me/a/1"}, {"title": 5, "post_url": []}]'
    items, _ = parse_findings(raw)
    assert [i["title"] for i in items] == ["Первый"], items


def test_unknown_fields_are_ignored_not_fatal():
    items, _ = parse_findings(_answer(_finding(relevance=0.9, extra="что-то")))
    assert len(items) == 1 and "relevance" not in items[0], items


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------


def test_the_same_vacancy_in_three_channels_becomes_one():
    groups = [
        {"chat_title": "alpha", "items": [_finding()]},
        {"chat_title": "beta", "items": [_finding()]},
        {"chat_title": "gamma", "items": [_finding()]},
    ]
    assert len(merge_findings(groups)) == 1, "дубль по ссылке не схлопнулся"


def test_a_duplicate_is_caught_even_when_the_link_differs():
    """Один и тот же пост перепечатан в другом канале — ссылка другая."""
    groups = [
        {"chat_title": "alpha", "items": [_finding()]},
        {"chat_title": "beta", "items": [_finding(post_url="https://t.me/beta/9")]},
    ]
    assert len(merge_findings(groups)) == 1, "дубль по названию и компании остался"


def test_the_fuller_record_wins_the_duplicate():
    thin = _finding(salary="", location="", post_url="https://t.me/beta/9")
    groups = [
        {"chat_title": "alpha", "items": [thin]},
        {"chat_title": "beta", "items": [_finding()]},
    ]
    merged = merge_findings(groups)
    assert merged[0]["salary"] == "от 100 000 ₽", (
        f"при дубле осталась более бедная запись: {merged[0]}"
    )


def test_channel_order_is_the_order_of_the_message():
    groups = [
        {
            "chat_title": "alpha",
            "items": [_finding(title="А", post_url="https://t.me/a/1")],
        },
        {
            "chat_title": "beta",
            "items": [_finding(title="Б", post_url="https://t.me/b/1")],
        },
    ]
    assert [i["title"] for i in merge_findings(groups)] == ["А", "Б"]


# --------------------------------------------------------------------------
# Отрисовка
# --------------------------------------------------------------------------


def test_foreign_text_is_escaped_not_trusted():
    """Название компании пишем не мы: знак `<` ломал разбор у Bot API (9.17)."""
    out = render_findings([_finding(company="Рога & <b>Копыта</b>")])
    assert "&amp;" in out and "&lt;b&gt;" in out, out


def test_empty_fields_do_not_leave_holes():
    out = render_findings([_finding(salary="", location="", format="")])
    assert "не указано" not in out.lower(), f"пустое поле нарисовано словами: {out}"
    assert out.count("()") == 0 and " — \n" not in out, (
        f"пустое поле оставило дыру: {out}"
    )


def test_every_finding_keeps_its_link():
    out = render_findings([_finding()])
    assert 'href="https://t.me/alpha/1"' in out, out


# --------------------------------------------------------------------------
# Воркер: режим сборки
# --------------------------------------------------------------------------


async def _template_source(db, user, channels):
    return await _source(
        db, user, channels=channels, answer_prompt="оформить", assembly_mode="template"
    )


async def test_the_template_mode_never_calls_the_model_twice(db, user):
    """Антивакуум: второго вызова нет вовсе — это весь смысл задачи."""
    source = await _template_source(
        db, user, [("@alpha", -1001, 20, "искать"), ("@beta", -1002, 20, "искать")]
    )
    llm = RecordingLLM(default=_answer(_finding()))
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    assert len(llm.calls) == 2, (
        f"вызовов {len(llm.calls)} при двух каналах — сведение вернулось: "
        f"{[c['prompt'][:40] for c in llm.calls]}"
    )


async def test_the_findings_are_stored_as_data(db, user):
    """В строке ленты лежат данные, а не только их отрисовка."""
    from test_57_dispatch import Recorder, _enable_all

    from app.services.dispatch import dispatch as real_dispatch

    source = await _template_source(db, user, [("@alpha", -1001, 20, "искать")])
    await _enable_all(db, user.id)
    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=real_dispatch)
    worker.llm = RecordingLLM(default=_answer(_finding()))

    await _run(db, source, worker, bot_sender=Recorder(result=True))

    card = (await db.scalars(select(FeedItem))).first()
    assert card is not None and card.findings_json, "находки не сохранены как данные"
    stored = json.loads(card.findings_json)
    assert stored[0]["post_url"] == "https://t.me/alpha/1", stored


async def test_a_channel_that_answers_off_schema_keeps_its_text(db, user):
    """Находки дороже схемы: ответ не по контракту сохраняется как есть."""
    source = await _template_source(db, user, [("@alpha", -1001, 20, "искать")])
    dispatcher = RecordingDispatcher()
    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=dispatcher)
    worker.llm = RecordingLLM(default="Ничего не нашёл, но вот мысли по каналу")

    await _run(db, source, worker)

    analysis = dispatcher.calls[0]["analysis"]
    assert "мысли по каналу" in analysis, f"текст канала потерян: {analysis!r}"
    assert "alpha" in analysis, f"канал не назван: {analysis!r}"
    events = [e.event_type for e in await db.scalars(select(LogEntry))]
    assert "AI_OFF_SCHEMA" in events, f"ответ не по схеме нигде не отмечен: {events}"


# --------------------------------------------------------------------------
# Схема уходит провайдеру — и не убивает модели, которые её не умеют
# --------------------------------------------------------------------------


async def _enable_ai(db, user_id):
    from test_126_usage_by_channel import _enable_ai as enable

    await enable(db, user_id)


async def test_the_schema_travels_to_the_provider(db, user):
    """Строгий режим — единственное, что делает формат ответа обязательным."""
    from app.services.llm import process_messages_batch_with_llm

    seen: list[dict] = []

    async def caller(payload):
        seen.append(payload)
        return _answer(_finding()), 10

    await _enable_ai(db, user.id)
    await process_messages_batch_with_llm(
        db,
        user.id,
        [{"id": 1, "text": "пост", "post_url": "https://t.me/a/1"}],
        caller=caller,
        require_success=True,
        schema=response_schema(),
    )
    sent = seen[0]
    assert sent.get("response_format", {}).get("type") == "json_schema", (
        f"схема провайдеру не ушла: {sent.keys()}"
    )
    assert sent["response_format"]["json_schema"].get("strict") is True, sent
    assert sent.get("provider", {}).get("require_parameters") is True, (
        "без require_parameters OpenRouter уведёт запрос к провайдеру, "
        "который параметр молча игнорирует"
    )


async def test_a_model_that_cannot_do_schemas_still_works(db, user):
    """Иначе смена модели в настройках молча убила бы все источники."""
    import httpx

    from app.services.llm import process_messages_batch_with_llm

    seen: list[dict] = []

    async def caller(payload):
        seen.append(dict(payload))
        if "response_format" in payload:
            raise httpx.HTTPStatusError(
                "400: response_format is not supported",
                request=httpx.Request("POST", "https://openrouter.ai"),
                response=httpx.Response(
                    400, json={"error": {"message": "response_format not supported"}}
                ),
            )
        return _answer(_finding()), 10

    await _enable_ai(db, user.id)
    result = await process_messages_batch_with_llm(
        db,
        user.id,
        [{"id": 1, "text": "пост", "post_url": "https://t.me/a/1"}],
        caller=caller,
        require_success=True,
        schema=response_schema(),
    )
    assert result, "запрос не повторили без схемы — источник остался без находок"
    assert len(seen) == 2, f"повторов {len(seen) - 1}, ожидался ровно один: {seen}"
    assert "response_format" not in seen[1], "повтор ушёл с той же схемой"


async def test_a_real_failure_is_not_swallowed_by_the_retry(db, user):
    """Антивакуум: повтор — только про схему, остальные отказы остаются отказами.

    Ошибка первой версии теста, исправлена в тесте: она ждала наружу
    `HTTPStatusError`, а модуль намеренно заворачивает отказ провайдера в
    `RuntimeError` — батч возвращается в очередь целиком, посты сохранены
    (контракт 9.1). Проверять надо не тип исключения, а ЧИСЛО ЗАПРОСОВ: отказ
    по деньгам не должен выглядеть как отказ по схеме и оплачиваться дважды.
    """
    import httpx

    from app.services.llm import process_messages_batch_with_llm

    calls: list[dict] = []

    async def caller(payload):
        calls.append(dict(payload))
        raise httpx.HTTPStatusError(
            "402: insufficient credits",
            request=httpx.Request("POST", "https://openrouter.ai"),
            response=httpx.Response(402, json={"error": {"message": "no credits"}}),
        )

    await _enable_ai(db, user.id)
    with pytest.raises(RuntimeError):
        await process_messages_batch_with_llm(
            db,
            user.id,
            [{"id": 1, "text": "пост", "post_url": "https://t.me/a/1"}],
            caller=caller,
            require_success=True,
            schema=response_schema(),
        )
    assert len(calls) == 1, f"отказ по деньгам приняли за отказ по схеме: {calls}"


# --------------------------------------------------------------------------
# Поверхности: API и кабинет
# --------------------------------------------------------------------------


async def test_the_api_carries_the_mode_and_defaults_to_the_old_one(
    anon_client, db, user
):
    """Умолчание прежнее: у существующих источников промпт уже написан."""
    from conftest import act_as

    await act_as(anon_client, db, user)
    created = await anon_client.post(
        "/api/sources", json={"title": "Источник", "interval_minutes": 60}
    )
    assert created.json()["assembly_mode"] == "prompt", created.json()

    public_id = created.json()["public_id"]
    changed = await anon_client.patch(
        f"/api/sources/{public_id}", json={"assembly_mode": "template"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["assembly_mode"] == "template", changed.json()


async def test_an_unknown_mode_is_refused(anon_client, db, user):
    """Опечатка в режиме не должна тихо оставлять источник на старом пути."""
    from conftest import act_as

    await act_as(anon_client, db, user)
    created = await anon_client.post("/api/sources", json={"title": "Источник"})
    public_id = created.json()["public_id"]
    bad = await anon_client.patch(
        f"/api/sources/{public_id}", json={"assembly_mode": "шаблон"}
    )
    assert bad.status_code in (400, 422), bad.text


async def test_the_feed_hands_out_findings_as_data(anon_client, db, user):
    """Кабинету и n8n нужны данные, а не разбор чужого текста."""
    from conftest import act_as

    from app.models import FeedItem

    db.add(
        FeedItem(
            user_id=user.id,
            job_id="job-findings",
            chat_title="ТОП-вакансии",
            messages_count=1,
            ai_analysis="<b>ок</b>",
            findings_json=json.dumps([_finding()], ensure_ascii=False),
        )
    )
    await db.commit()

    await act_as(anon_client, db, user)
    listed = await anon_client.get("/api/feed?limit=10")
    assert listed.status_code == 200, listed.text
    rows = listed.json().get("items") or listed.json().get("feed") or []
    row = next(r for r in rows if r["job_id"] == "job-findings")
    assert row.get("findings"), f"находки не отдаются лентой: {row.keys()}"
    assert row["findings"][0]["post_url"] == "https://t.me/alpha/1", row["findings"]


def test_the_cabinet_draws_findings_from_data():
    """Карточка рисуется из находок, а старые записи остаются на тексте."""
    from pathlib import Path

    feed_js = (
        Path(__file__).resolve().parents[1] / "static" / "js" / "feed.js"
    ).read_text(encoding="utf-8")
    assert "findings" in feed_js, "кабинет не знает про находки как данные"
    assert "ai_analysis" in feed_js, (
        "запасной путь убран — записи ленты до 13.9 опустеют"
    )


def test_the_form_offers_the_mode():
    from pathlib import Path

    index = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for element_id in ("sourceAssemblyMode", "editSourceAssemblyMode"):
        assert f'id="{element_id}"' in index, (
            f"переключателя режима нет в разметке: {element_id}"
        )
        assert 'value="template"' in index, "режим сборки кодом нельзя выбрать"
