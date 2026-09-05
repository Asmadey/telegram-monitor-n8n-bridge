"""Задача 5.4 — аватарки из строк ленты (PLAN.md).

Старая лента (server.py:1842) возила в КАЖДОЙ строке GET /api/feed и
photo_base64 (data:image), и raw_messages_json целиком: двести аватарок —
мегабайты на каждый опрос ленты (она опрашивается каждые 30 секунд).
Новая сборка: список — только метаданные карточек, исходные посты —
детальным видом GET /api/feed/{id}, аватарка — отдельным
GET /api/avatars/{chat_id} с Cache-Control.

Уровни проверок:
- структурные: модель ChatAvatar (без user_id — фото публичного канала
  одно на всех, изоляция на чтении через монитор юзера), миграция
  создаёт chat_avatars и цепляется за 0002, фронт берёт аватарку с
  эндпоинта, а не из строки ленты;
- поведенческие (ASGITransport + временная aiosqlite): красный тест
  плана — ответ GET /api/feed?limit=50 весит меньше 200 КБ и не
  содержит data:image, даже когда в raw_messages_json сидят килобайты
  инлайн-картинок; аватарка отдаётся владельцу монитора с Cache-Control,
  чужому — 404; детальный вид несёт messages, чужой id — 404.
"""

import json
import pathlib
import re

import pytest

from tests.conftest import act_as

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 50 строк × 6 КБ data:image в raw_messages_json: если список возит
# raw_messages_json (или аватарки), ответ уезжает за 300 КБ — за
# красную границу 200 КБ из плана
BLOB = "data:image/jpeg;base64," + "A" * 6000
CHAT_ID = 424242001
AVATAR_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-of-channel"


# --------------------------------------------------------------------------
# Структурные
# --------------------------------------------------------------------------


def test_chat_avatar_model_exists_and_is_global():
    """chat_avatars: (chat_id PK, image_bytes, fetched_at) и БЕЗ user_id —
    фото публичного канала одно на всех, дублировать по тенантам нельзя.
    Изоляция держится на чтении: эндпоинт отдаёт bytes только юзеру,
    который мониторит этот канал (поведенческий тест ниже)."""
    from app import models

    cls = getattr(models, "ChatAvatar", None)
    assert cls is not None, "app.models.ChatAvatar не существует"
    cols = cls.__table__.columns
    assert "chat_id" in cols and "image_bytes" in cols and "fetched_at" in cols
    assert cols["chat_id"].primary_key, "chat_id — PK (одна аватарка на канал)"
    assert "user_id" not in cols, (
        "chat_avatars не тенантная таблица: user_id здесь размножил бы фото"
    )


def test_migration_creates_chat_avatars():
    """Схема — только миграциями (задача 1.4): у chat_avatars обязана быть
    своя ревизия, цепочка миграций неразрывна (0003 за 0002)."""
    versions = ROOT / "alembic" / "versions"
    hits = [
        f
        for f in versions.glob("*.py")
        if "create_table" in f.read_text(encoding="utf-8")
        and '"chat_avatars"' in f.read_text(encoding="utf-8")
    ]
    assert hits, "нет ревизии, создающей chat_avatars"
    src = hits[0].read_text(encoding="utf-8")
    assert re.search(r'down_revision\s*=\s*["\']0002_llm_usage["\']', src), (
        "ревизия chat_avatars не встраивается в цепочку за 0002_llm_usage"
    )


def test_feed_js_uses_avatar_endpoint():
    """Фронт берёт аватарку с GET /api/avatars/{chat_id} (с кешом браузера),
    а битый img не оставляет: нет строки в chat_avatars / 404 — буква.
    Ветку photo_base64 сохраняем до закрытия К2: монолит (server.py) ещё
    рантайм и отдёт аватарки строкой ленты."""
    src = (ROOT / "static" / "js" / "feed.js").read_text(encoding="utf-8")
    assert "/api/avatars/" in src, "feed.js не ходит за аватарками на эндпоинт"
    assert "addEventListener('error'" in src, (
        "нет fallback на букву: битый img остаётся дырой вместо аватарки"
    )


# --------------------------------------------------------------------------
# Поведенческие
# --------------------------------------------------------------------------


def _seed_feed_item(
    db, user_id: int, *, job_id: str, chat_id: int, title: str, analysis: str, raw: str
) -> None:
    from app.models import FeedItem

    db.add(
        FeedItem(
            user_id=user_id,
            job_id=job_id,
            chat_id=chat_id,
            chat_title=title,
            messages_count=1,
            ai_analysis=analysis,
            raw_messages_json=raw,
        )
    )


@pytest.mark.asyncio
async def test_feed_list_is_small_and_has_no_inline_images(anon_client, db, user):
    """Красный тест из плана: GET /api/feed?limit=50 — меньше 200 КБ и без
    data:image, даже когда в raw_messages_json каждой строки сидит
    килобайтная инлайн-картинка. Список отдаёт ТОЛЬКО метаданные карточек."""
    from app.models import FeedItem

    for i in range(50):
        db.add(
            FeedItem(
                user_id=user.id,
                job_id=f"job-{i:03d}",
                chat_id=CHAT_ID,
                chat_title=f"Канал {i}",
                messages_count=1,
                ai_analysis="сводка",
                raw_messages_json=json.dumps(
                    [{"id": 1, "text": BLOB}], ensure_ascii=False
                ),
            )
        )
    await db.commit()

    await act_as(anon_client, db, user)
    resp = await anon_client.get("/api/feed?limit=50")
    assert resp.status_code == 200, (
        f"GET /api/feed → {resp.status_code}: роут ленты ещё не перенесён из server.py"
    )
    assert len(resp.content) < 200 * 1024, (
        f"ответ ленты {len(resp.content)} байт — список возит тяжёлые поля"
    )
    assert "data:image" not in resp.text, "в списочном ответе есть инлайн-картинки"


@pytest.mark.asyncio
async def test_feed_detail_returns_messages_and_hides_foreign(
    anon_client, db, user, user_b
):
    """raw_messages_json нужен только детальному виду: GET /api/feed/{id}
    отдаёт распарсенные messages. Чужой id — 404 (None из TenantRepo),
    не 403: 403 подтверждает существование."""
    _seed_feed_item(
        db,
        user.id,
        job_id="job-a",
        chat_id=CHAT_ID,
        title="Канал A",
        analysis="сводка A",
        raw=json.dumps([{"id": 7, "text": "исходный пост"}], ensure_ascii=False),
    )
    _seed_feed_item(
        db,
        user_b.id,
        job_id="job-b",
        chat_id=CHAT_ID + 1,
        title="Канал B",
        analysis="сводка B",
        raw="[]",
    )
    await db.commit()

    await act_as(anon_client, db, user)
    listed_resp = await anon_client.get("/api/feed")
    assert listed_resp.status_code == 200, (
        f"GET /api/feed → {listed_resp.status_code}: роут ленты ещё не перенесён из server.py"
    )
    listed = listed_resp.json()
    assert listed["feed"], "список пуст — сидинг не работает"
    id_a = listed["feed"][0]["id"]
    id_b = [f["id"] for f in listed["feed"] if f["job_id"] == "job-b"]
    assert not id_b, "в списке юзера A чужая строка юзера B"

    resp = await anon_client.get(f"/api/feed/{id_a}")
    assert resp.status_code == 200, f"свой детальный вид → {resp.status_code}"
    item = resp.json()["feed_item"]
    assert item["messages"][0]["text"] == "исходный пост", (
        "детальный вид не несёт исходные посты"
    )
    assert "raw_messages_json" not in item, "сырой JSON наружу не отдаётся"


@pytest.mark.asyncio
async def test_avatar_for_owner_with_cache_control_and_404_for_stranger(
    anon_client, db, user, user_b
):
    """Аватарка: владельцу монитора — 200 image/jpeg + Cache-Control (ради
    кеша и затеяно), чужому юзеру и «каналу без аватарки» — 404: эндпоинт
    не подтверждает существование канала произвольным клиентам."""
    from app import models

    ChatAvatar = getattr(models, "ChatAvatar", None)
    assert ChatAvatar is not None, "app.models.ChatAvatar не существует (задача 5.4)"
    Monitor = models.Monitor

    db.add(Monitor(user_id=user.id, chat_target="@a", chat_id=CHAT_ID))
    db.add(ChatAvatar(chat_id=CHAT_ID, image_bytes=AVATAR_BYTES))
    # канал мониторится, но фото не скачано (воркер ещё не ходил) — тоже 404
    db.add(Monitor(user_id=user.id, chat_target="@empty", chat_id=CHAT_ID + 1))
    await db.commit()

    await act_as(anon_client, db, user)
    resp = await anon_client.get(f"/api/avatars/{CHAT_ID}")
    assert resp.status_code == 200, f"владельцу монитора → {resp.status_code}"
    assert resp.content == AVATAR_BYTES
    assert resp.headers["content-type"].startswith("image/jpeg")
    cache = resp.headers.get("cache-control", "")
    assert "private" in cache and "max-age" in cache, (
        f"без Cache-Control браузер не кеширует: {cache!r}"
    )

    empty = await anon_client.get(f"/api/avatars/{CHAT_ID + 1}")
    assert empty.status_code == 404, "нет строки аватарки → не 200"

    # чужой юзер НЕ мониторит этот канал — 404, не 403 (существование не раскрываем)
    await act_as(anon_client, db, user_b)
    stranger = await anon_client.get(f"/api/avatars/{CHAT_ID}")
    assert stranger.status_code == 404, (
        f"чужому → {stranger.status_code}: аватарка публичного канала доступна"
        " только тем, кто его мониторит"
    )
