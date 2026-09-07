"""К2 — перенос каналов мониторинга из server.py в app/api/monitors.py.

Шесть эндпоинтов монолита (server.py:1277-1527) без какой-либо авторизации:
список, добавление, правка, удаление, немедленный запуск и сброс дедупликации.
Пока они живут в server.py, живой деплой невозможен — любой прохожий читает
и меняет чужие каналы.

Отличия порта от оригинала, ради которых задача и существует:

- роутер целиком за require_user (закрыто по умолчанию, 2.3), тенантные
  чтения только через TenantRepo — чужой канал даёт 404, не 403;
- адресация по public_id, а не по первичному ключу: последовательный id
  выдаёт число чужих строк, а public_id к тому же совпадает со старым
  TEXT-id из SQLite, так что ссылки переживают перенос данных (1.5);
- «Запустить сейчас» не опрашивает Telegram внутри HTTP-запроса, как делал
  монолит (server.py:1446), а кладёт задачу в очередь (4.3): долгоживущие
  Telethon-клиенты принадлежат только воркеру, иначе второй клиент на том же
  auth-key выбьет пользователя из его аккаунта (AUTH_KEY_DUPLICATED);
- разрешение канала в Telegram — инъектируемая зависимость, как у входа
  (get_telegram_auth_client): тест подставляет свою и не ходит в сеть.
"""

import pytest
from conftest import act_as

pytestmark = pytest.mark.asyncio


async def _login(client, db, user):
    """Вход через общий помощник conftest: он ставит и сессию, и csrf-токен,
    привязанный к sid, — иначе не-GET уходит в 403 (задача 2.6)."""
    await act_as(client, db, user)
    return client


class FakeEntity:
    """То, что Telethon возвращает на get_entity: канал с id/названием."""

    def __init__(self, chat_id=-1001143063102, title="Канал", username="channel"):
        self.id = chat_id
        self.title = title
        self.username = username


@pytest.fixture
def fake_resolver(app):
    """Подменяет разрешение канала: тест не ходит в Telegram."""
    from app.api.monitors import get_entity_resolver

    async def resolver(target: str):
        return FakeEntity(title=f"Канал {target}", username=target.lstrip("@"))

    app.dependency_overrides[get_entity_resolver] = lambda: resolver
    yield resolver
    app.dependency_overrides.pop(get_entity_resolver, None)


async def _add(client, target="@channel0", **extra):
    payload = {"chat_target": target, **extra}
    return await client.post("/api/monitors", json=payload)


# --------------------------------------------------------------------------
# Список и добавление
# --------------------------------------------------------------------------


async def test_add_and_list_monitor(anon_client, db, user, fake_resolver):
    await _login(anon_client, db, user)

    created = await _add(anon_client, "@channel0", interval_minutes=30, limit=15)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["chat_title"] == "Канал @channel0"
    assert body["interval_minutes"] == 30
    assert body["limit"] == 15
    assert body["public_id"], "канал без public_id — по нему не адресоваться"

    listed = await anon_client.get("/api/monitors")
    assert listed.status_code == 200
    monitors = listed.json()["monitors"]
    assert [m["public_id"] for m in monitors] == [body["public_id"]]


async def test_same_channel_cannot_be_added_twice(anon_client, db, user, fake_resolver):
    await _login(anon_client, db, user)
    assert (await _add(anon_client, "@channel0")).status_code == 201
    again = await _add(anon_client, "@channel0")
    assert again.status_code == 400, "дубль канала обязан отклоняться"


async def test_two_users_may_watch_the_same_channel(
    anon_client, second_client, db, user_a, user_b, fake_resolver
):
    """Запрет дубля — в пределах пользователя, а не глобально.

    Глобальный запрет означал бы, что первый подписавшийся на канал
    занимает его для всего сервиса.
    """
    await _login(anon_client, db, user_a)
    assert (await _add(anon_client, "@shared")).status_code == 201

    await _login(second_client, db, user_b)
    second = await _add(second_client, "@shared")
    assert second.status_code == 201, (
        "второй пользователь не смог добавить тот же канал — "
        f"{second.status_code}: запрет дубля должен быть в пределах тенанта"
    )


# --------------------------------------------------------------------------
# Правка, удаление
# --------------------------------------------------------------------------


async def test_patch_updates_only_given_fields(anon_client, db, user, fake_resolver):
    await _login(anon_client, db, user)
    pid = (await _add(anon_client, "@channel0")).json()["public_id"]

    patched = await anon_client.patch(
        f"/api/monitors/{pid}", json={"interval_minutes": 120, "is_active": False}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["interval_minutes"] == 120
    assert body["is_active"] is False
    assert body["limit"] == 20, "не переданное поле не должно меняться"


async def test_delete_removes_monitor(anon_client, db, user, fake_resolver):
    await _login(anon_client, db, user)
    pid = (await _add(anon_client, "@channel0")).json()["public_id"]

    assert (await anon_client.delete(f"/api/monitors/{pid}")).status_code == 200
    assert (await anon_client.get("/api/monitors")).json()["monitors"] == []
    assert (await anon_client.delete(f"/api/monitors/{pid}")).status_code == 404


# --------------------------------------------------------------------------
# Запуск и сброс дедупликации
# --------------------------------------------------------------------------


async def test_run_enqueues_a_job_instead_of_polling_inline(
    anon_client, db, user, fake_resolver
):
    """Ключевое отличие от монолита: HTTP-запрос не держит Telethon.

    В server.py «Запустить» опрашивал Telegram прямо в обработчике. В новой
    сборке долгоживущими клиентами владеет только воркер, поэтому веб кладёт
    задачу в очередь и отвечает 202.
    """
    from sqlalchemy import select

    from app.models import Job

    await _login(anon_client, db, user)
    pid = (await _add(anon_client, "@channel0")).json()["public_id"]

    started = await anon_client.post(f"/api/monitors/{pid}/run")
    assert started.status_code == 202, started.text
    assert started.json()["job_id"]

    jobs = (await db.scalars(select(Job).where(Job.user_id == user.id))).all()
    assert len(jobs) == 1, "задача не попала в очередь"
    assert jobs[0].kind == "poll_monitor"


async def test_reset_dedup_clears_only_this_users_history(
    anon_client, db, user_a, user_b, fake_resolver
):
    """Сброс дедупликации не имеет права трогать историю другого тенанта."""
    from sqlalchemy import func, select

    from app.models import SentMessage

    chat_id = -1001143063102
    for owner in (user_a, user_b):
        db.add(
            SentMessage(
                user_id=owner.id, chat_id=chat_id, message_id=1, reactions_json="[]"
            )
        )
    await db.commit()

    await _login(anon_client, db, user_a)
    pid = (await _add(anon_client, "@channel0")).json()["public_id"]

    reset = await anon_client.post(f"/api/monitors/{pid}/reset-dedup")
    assert reset.status_code == 200, reset.text

    left = await db.scalar(
        select(func.count())
        .select_from(SentMessage)
        .where(SentMessage.chat_id == chat_id)
    )
    assert left == 1, "сброс задел историю другого пользователя"


# --------------------------------------------------------------------------
# Чужие каналы
# --------------------------------------------------------------------------


async def test_foreign_monitor_is_404_everywhere(
    anon_client, second_client, db, user_a, user_b, fake_resolver
):
    """На чужой ресурс всегда 404: 403 подтверждает, что он существует."""
    await _login(anon_client, db, user_a)
    pid = (await _add(anon_client, "@private")).json()["public_id"]

    await _login(second_client, db, user_b)
    for method, url in (
        ("PATCH", f"/api/monitors/{pid}"),
        ("DELETE", f"/api/monitors/{pid}"),
        ("POST", f"/api/monitors/{pid}/run"),
        ("POST", f"/api/monitors/{pid}/reset-dedup"),
    ):
        resp = await second_client.request(method, url, json={})
        assert resp.status_code == 404, (
            f"{method} {url} → {resp.status_code}, ожидался 404"
        )
