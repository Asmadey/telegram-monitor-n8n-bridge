"""API источников: несколько каналов у одной задачи поиска (11.6).

До сих пор единственным способом завести канал был `/api/monitors`, где
источник равен каналу. Пять каналов с общими критериями собрать было
нечем — ради этого и затевалась фаза.

Правила, которые здесь закрепляются, вытекают из аудита плана, а не из
абстрактной аккуратности:

- **потолки** (10 каналов, 8000 символов промпта, 100 стоп-слов) — без них
  прогон растягивается на часы, а счёт за токены не ограничен ничем;
- **запуск идемпотентен**: повторный `POST .../run` при живой задаче
  возвращает ЕЁ идентификатор, а не создаёт вторую. Два тапа по кнопке —
  это двойной счёт за токены и две доставки, и защищаться от этого надо на
  сервере: кнопка про чужой сеанс ничего не знает;
- **дубль канала в источнике — 409**, а не тихое добавление: тот же канал
  дважды означает двойной опрос;
- **чужой источник — 404**, никогда 403 (403 подтверждает существование).
"""

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio


async def _source(anon_client, **body) -> dict:
    payload = {"title": "Вакансии продаж", "interval_minutes": 60}
    payload.update(body)
    response = await anon_client.post("/api/sources", json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _channel(anon_client, source_id: str, target: str, **body):
    payload = {"chat_target": target, "limit": 20, "extract_prompt": "искать"}
    payload.update(body)
    return await anon_client.post(f"/api/sources/{source_id}/channels", json=payload)


async def test_source_holds_several_channels(anon_client, db, user):
    """Главный сценарий фазы."""
    await act_as(anon_client, db, user)
    source = await _source(anon_client, stop_words="разработчик")

    for index, target in enumerate(("@alpha", "@beta", "@gamma")):
        created = await _channel(
            anon_client,
            source["public_id"],
            target,
            limit=(index + 1) * 10,
            extract_prompt=f"критерии {index}",
        )
        assert created.status_code in (200, 201), created.text

    listed = (await anon_client.get("/api/sources")).json()
    assert len(listed["sources"]) == 1
    channels = listed["sources"][0]["channels"]
    assert [c["chat_target"] for c in channels] == ["@alpha", "@beta", "@gamma"]
    assert [c["limit"] for c in channels] == [10, 20, 30], "лимиты перепутаны"
    assert channels[0]["extract_prompt"] == "критерии 0"


async def test_answer_is_addressed_by_public_id_only(anon_client, db, user):
    """Контракт 9.10: наружу уходит public_id, внутренний ключ — нет."""
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    assert source.get("public_id")
    assert "id" not in source, "во внешний ответ попал внутренний ключ строки"


async def test_duplicate_channel_is_rejected(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    await _channel(anon_client, source["public_id"], "@alpha")

    again = await _channel(anon_client, source["public_id"], "@alpha")
    assert again.status_code == 409, (
        f"тот же канал добавлен дважды ({again.status_code}) — источник будет "
        "опрашивать его два раза и платить дважды"
    )


async def test_same_channel_in_another_source_is_allowed(anon_client, db, user):
    """Решение владельца: канал может входить в несколько источников."""
    await act_as(anon_client, db, user)
    first = await _source(anon_client, title="Продажи")
    second = await _source(anon_client, title="Аналитика")
    await _channel(anon_client, first["public_id"], "@alpha")

    added = await _channel(anon_client, second["public_id"], "@alpha")
    assert added.status_code in (200, 201), added.text


async def test_channel_ceiling(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    for index in range(10):
        assert (
            await _channel(anon_client, source["public_id"], f"@ch{index}")
        ).status_code in (200, 201)

    extra = await _channel(anon_client, source["public_id"], "@overflow")
    assert extra.status_code == 400, "потолок каналов не соблюдается"
    assert "10" in extra.json()["detail"], "в отказе не сказано, каков потолок"


async def test_prompt_ceiling(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    too_long = await _channel(
        anon_client, source["public_id"], "@alpha", extract_prompt="я" * 9000
    )
    assert too_long.status_code == 400, "промпт без потолка — счёт без потолка"


async def test_run_is_idempotent(anon_client, db, user):
    """Два тапа по кнопке не должны стоить вдвое."""
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    await _channel(anon_client, source["public_id"], "@alpha")

    first = await anon_client.post(f"/api/sources/{source['public_id']}/run")
    second = await anon_client.post(f"/api/sources/{source['public_id']}/run")

    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"], (
        "второй запуск создал вторую задачу: двойной счёт за токены и две "
        "доставки одного и того же"
    )


async def test_status_shows_the_breakdown(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    await _channel(anon_client, source["public_id"], "@alpha")

    status = await anon_client.get(f"/api/sources/{source['public_id']}/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert "running" in body and "channels" in body
    assert body["channels"][0]["chat_target"] == "@alpha"


async def test_foreign_source_is_invisible(anon_client, db, user, user_b):
    """404, а не 403: 403 подтверждает существование объекта."""
    await act_as(anon_client, db, user)
    source = await _source(anon_client)

    await act_as(anon_client, db, user_b)
    # У добавления канала тело обязательное: с пустым запрос отбивается
    # проверкой формы (422) ДО проверки владельца, и тест мерил бы не то.
    # Существование источника это не выдаёт — 422 приходит на любой пустой
    # запрос, но проверять надо именно 404 на чужой объект.
    cases = [
        ("GET", f"/api/sources/{source['public_id']}/status", None),
        ("PATCH", f"/api/sources/{source['public_id']}", {}),
        ("DELETE", f"/api/sources/{source['public_id']}", None),
        ("POST", f"/api/sources/{source['public_id']}/run", None),
        (
            "POST",
            f"/api/sources/{source['public_id']}/channels",
            {"chat_target": "@alpha", "limit": 20, "extract_prompt": ""},
        ),
    ]
    for method, url, body in cases:
        response = await anon_client.request(
            method, url, **({"json": body} if body is not None else {})
        )
        assert response.status_code == 404, f"{method} {url} → {response.status_code}"


async def test_channel_can_be_removed(anon_client, db, user):
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    added = (await _channel(anon_client, source["public_id"], "@alpha")).json()

    removed = await anon_client.delete(
        f"/api/sources/{source['public_id']}/channels/{added['channel_id']}"
    )
    assert removed.status_code == 200, removed.text
    listed = (await anon_client.get("/api/sources")).json()
    assert listed["sources"][0]["channels"] == []


async def test_patch_updates_only_given_fields(anon_client, db, user):
    """Перенесено из набора снятого `/api/monitors` (11.8).

    Частичная правка не должна обнулять соседние поля: форма присылает то,
    что изменил пользователь, а не всю карточку целиком.
    """
    await act_as(anon_client, db, user)
    source = await _source(
        anon_client, stop_words="разработчик", answer_prompt="оформить кратко"
    )

    patched = await anon_client.patch(
        f"/api/sources/{source['public_id']}", json={"interval_minutes": 360}
    )
    body = patched.json()
    assert body["interval_minutes"] == 360
    assert body["stop_words"] == "разработчик", "правка интервала стёрла стоп-слова"
    assert body["answer_prompt"] == "оформить кратко", "правка стёрла промпт"


async def test_reset_dedup_touches_only_this_source(anon_client, db, user):
    """Перенесено из набора снятого `/api/monitors` (11.8).

    Сброс истории у одного источника не должен трогать соседний, который
    следит за тем же каналом: иначе ему прилетят сотни старых постов.
    """
    from sqlalchemy import select

    from app.models import Monitor, SentMessage
    from app.services.dedup import filter_new

    await act_as(anon_client, db, user)
    first = await _source(anon_client, title="Продажи")
    second = await _source(anon_client, title="Аналитика")
    ids = {s.public_id: s.id for s in await db.scalars(select(Monitor))}
    posts = [{"id": 11, "text": "пост"}]
    for public_id in (first["public_id"], second["public_id"]):
        await filter_new(db, user.id, -1001, posts, monitor_id=ids[public_id])

    await anon_client.post(f"/api/sources/{first['public_id']}/reset-dedup")

    left = list(await db.scalars(select(SentMessage.monitor_id)))
    assert left == [ids[second["public_id"]]], (
        f"сброс задел чужую историю: осталось {left}"
    )


async def test_editing_a_channel_prompt_persists_and_comes_back(anon_client, db, user):
    """Правка промпта канала — то самое действие владельца (2026-09-10).

    Набор проверял СОЗДАНИЕ канала с промптом и потолок его длины, но не
    ПРАВКУ: путь, которым пользуются каждый день, оставался непокрытым. Когда
    сохранение упало в интерфейсе (`test_121`), по тестам нельзя было сказать,
    доехал промпт до базы или нет, — а вопрос владельца был именно такой.
    """
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    created = (
        await _channel(
            anon_client,
            source["public_id"],
            "@alpha",
            limit=20,
            extract_prompt="старый",
        )
    ).json()

    patched = await anon_client.patch(
        f"/api/sources/{source['public_id']}/channels/{created['channel_id']}",
        json={"extract_prompt": "искать вакансии продаж"},
    )
    assert patched.status_code == 200, patched.text

    channels = (await anon_client.get("/api/sources")).json()["sources"][0]["channels"]
    assert channels[0]["extract_prompt"] == "искать вакансии продаж"
    assert channels[0]["limit"] == 20, "правка промпта сбросила лимит канала"


async def test_every_channel_of_a_source_can_be_edited(anon_client, db, user):
    """Правки доезжают до КАЖДОГО канала, а не только до первого.

    Дефект в интерфейсе обрывал обход на кнопке внутри первой строки: канал №1
    сохранялся, остальные — нет, молча. Здесь закреплено серверное свойство,
    на которое интерфейс обязан опираться.
    """
    await act_as(anon_client, db, user)
    source = await _source(anon_client)
    ids = []
    for index in range(3):
        created = await _channel(
            anon_client, source["public_id"], f"@ch{index}", extract_prompt="старый"
        )
        ids.append(created.json()["channel_id"])

    for index, channel_id in enumerate(ids):
        response = await anon_client.patch(
            f"/api/sources/{source['public_id']}/channels/{channel_id}",
            json={"extract_prompt": f"критерии {index}", "limit": (index + 1) * 5},
        )
        assert response.status_code == 200, response.text

    channels = (await anon_client.get("/api/sources")).json()["sources"][0]["channels"]
    assert [c["extract_prompt"] for c in channels] == [
        "критерии 0",
        "критерии 1",
        "критерии 2",
    ], "правки доехали не до всех каналов"
    assert [c["limit"] for c in channels] == [5, 10, 15]
