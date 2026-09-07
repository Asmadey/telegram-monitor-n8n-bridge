"""К2 — сохранённые посты и журнал событий (server.py:1252, 1794-1829).

Оба эндпоинта в монолите открыты. Опаснее прочих здесь `DELETE /api/logs`:
в server.py он выполняет `DELETE FROM logs` без единого условия. В
мульти-тенанте такой запрос стирает журнал ВСЕМ пользователям сервиса —
один клиент нажимает «очистить», остальные теряют историю. Порт обязан
удалять только свои строки, и это проверяется тестом.
"""

import json

import pytest
from conftest import act_as

from app.models import LogEntry, SentMessage

pytestmark = pytest.mark.asyncio


def _msg(user_id: int, chat_id: int, message_id: int, **extra) -> SentMessage:
    fields = {
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "sender": "Канал",
        "text": f"пост {message_id}",
        "views": 100,
        "forwards": 2,
        "has_media": False,
        "reactions_count": 3,
        "reactions_json": json.dumps([{"emoji": "🔥", "count": 3}]),
        "post_url": f"https://t.me/c/1/{message_id}",
    }
    fields.update(extra)
    return SentMessage(**fields)


# --------------------------------------------------------------------------
# Сохранённые посты
# --------------------------------------------------------------------------


async def test_messages_list_returns_own_posts_with_metrics(anon_client, db, user):
    db.add(_msg(user.id, -100500, 11))
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.get("/api/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["messages"][0]
    assert item["message_id"] == 11
    assert item["views"] == 100
    # реакции отдаются разобранным массивом, а не строкой JSON: иначе каждый
    # потребитель парсит их сам и по-своему
    assert item["reactions"] == [{"emoji": "🔥", "count": 3}]


async def test_messages_do_not_leak_between_tenants(anon_client, db, user_a, user_b):
    db.add(_msg(user_a.id, -100500, 11, text="секрет A"))
    db.add(_msg(user_b.id, -100600, 22, text="пост B"))
    await db.commit()
    await act_as(anon_client, db, user_b)

    body = (await anon_client.get("/api/messages")).json()
    assert body["total"] == 1
    assert "секрет A" not in json.dumps(body, ensure_ascii=False)


async def test_broken_reactions_json_does_not_break_the_tab(anon_client, db, user):
    """Битый JSON в одной строке не должен ронять весь список постов."""
    db.add(_msg(user.id, -100500, 11, reactions_json="{не json"))
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.get("/api/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"][0]["reactions"] == []


# --------------------------------------------------------------------------
# Журнал
# --------------------------------------------------------------------------


async def test_logs_list_and_status_filter(anon_client, db, user):
    db.add(
        LogEntry(
            user_id=user.id, event_type="WEBHOOK_SENT", status="SUCCESS", details="ок"
        )
    )
    db.add(
        LogEntry(
            user_id=user.id, event_type="POLL_ERROR", status="ERROR", details="сбой"
        )
    )
    await db.commit()
    await act_as(anon_client, db, user)

    all_logs = (await anon_client.get("/api/logs")).json()
    assert all_logs["total"] == 2

    errors = (await anon_client.get("/api/logs?status=ERROR")).json()
    assert [entry["status"] for entry in errors["logs"]] == ["ERROR"]


async def test_clear_logs_wipes_only_own_journal(anon_client, db, user_a, user_b):
    """Главное отличие от монолита: `DELETE FROM logs` без WHERE стёр бы
    журнал всем тенантам сразу."""
    from sqlalchemy import func, select

    db.add(LogEntry(user_id=user_a.id, event_type="X", status="INFO", details="A"))
    db.add(LogEntry(user_id=user_b.id, event_type="X", status="INFO", details="B"))
    await db.commit()

    await act_as(anon_client, db, user_a)
    cleared = await anon_client.delete("/api/logs")
    assert cleared.status_code == 200, cleared.text

    left_b = await db.scalar(
        select(func.count()).select_from(LogEntry).where(LogEntry.user_id == user_b.id)
    )
    assert left_b == 1, "очистка журнала задела другого пользователя"


async def test_clearing_logs_leaves_a_trace(anon_client, db, user):
    """После очистки журнал не пуст: сама очистка — событие, и её запись
    должна остаться, иначе действие невозможно отследить."""
    from sqlalchemy import func, select

    db.add(LogEntry(user_id=user.id, event_type="X", status="INFO", details="старое"))
    await db.commit()
    await act_as(anon_client, db, user)

    await anon_client.delete("/api/logs")
    left = await db.scalar(
        select(func.count()).select_from(LogEntry).where(LogEntry.user_id == user.id)
    )
    assert left == 1, "запись о самой очистке не сохранена"
