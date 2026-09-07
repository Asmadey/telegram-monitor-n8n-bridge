"""К2 — действия над лентой и маршруты вкладок SPA (server.py:1889-1990).

Лента читалась уже в задаче 5.4; здесь переносятся три действия — удаление
карточки, полная очистка и повторный анализ — плюс пять адресов вкладок,
которые монолит отдавал одним обработчиком (server.py:1979).

Повторный анализ, в отличие от монолита, не ходит в LLM внутри HTTP-запроса.
Вызов OpenRouter — это до 45 секунд по таймауту оригинала: держать на нём
воркер веб-процесса нельзя, поэтому задача уходит в очередь, как и опрос
канала (4.3), а лента и так обновляется живым опросом.
"""

import pytest
from conftest import act_as
from sqlalchemy import func, select

from app.models import FeedItem, Job

pytestmark = pytest.mark.asyncio

TABS = ["/feed", "/channels", "/messages", "/integration", "/logs"]


def _item(user_id: int, job_id: str, **extra) -> FeedItem:
    fields = {
        "user_id": user_id,
        "job_id": job_id,
        "chat_id": -100500,
        "chat_title": "Канал",
        "messages_count": 1,
        "ai_analysis": "сводка",
        "raw_messages_json": '[{"id": 1, "text": "пост"}]',
    }
    fields.update(extra)
    return FeedItem(**fields)


# --------------------------------------------------------------------------
# Удаление
# --------------------------------------------------------------------------


async def test_delete_one_card(anon_client, db, user):
    item = _item(user.id, "job-1")
    db.add(item)
    await db.commit()
    await act_as(anon_client, db, user)

    assert (await anon_client.delete(f"/api/feed/{item.id}")).status_code == 200
    assert (await anon_client.get("/api/feed")).json()["total"] == 0


async def test_clear_feed_wipes_only_own_cards(anon_client, db, user_a, user_b):
    """Как и с журналом: очистка без user_id стёрла бы ленту всем тенантам."""
    db.add(_item(user_a.id, "job-a"))
    db.add(_item(user_b.id, "job-b"))
    await db.commit()

    await act_as(anon_client, db, user_a)
    cleared = await anon_client.delete("/api/feed")
    assert cleared.status_code == 200, cleared.text

    left_b = await db.scalar(
        select(func.count()).select_from(FeedItem).where(FeedItem.user_id == user_b.id)
    )
    assert left_b == 1, "очистка ленты задела другого пользователя"


# --------------------------------------------------------------------------
# Повторный анализ
# --------------------------------------------------------------------------


async def test_reanalyze_enqueues_instead_of_calling_llm_inline(anon_client, db, user):
    item = _item(user.id, "job-1")
    db.add(item)
    await db.commit()
    await act_as(anon_client, db, user)

    started = await anon_client.post(f"/api/feed/{item.id}/reanalyze")
    assert started.status_code == 202, started.text

    jobs = (await db.scalars(select(Job).where(Job.user_id == user.id))).all()
    assert len(jobs) == 1
    assert jobs[0].kind == "reanalyze_feed_item"


async def test_reanalyze_refuses_a_card_without_source_posts(anon_client, db, user):
    """Пустая выборка — 400, а не задача, которая упадёт в воркере."""
    item = _item(user.id, "job-empty", raw_messages_json="[]")
    db.add(item)
    await db.commit()
    await act_as(anon_client, db, user)

    resp = await anon_client.post(f"/api/feed/{item.id}/reanalyze")
    assert resp.status_code == 400, resp.text
    assert (await db.scalar(select(func.count()).select_from(Job))) == 0


# --------------------------------------------------------------------------
# Вкладки SPA
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", TABS)
async def test_tab_url_serves_the_spa_shell(anon_client, path):
    """Прямой заход и перезагрузка на вкладке обязаны открывать приложение,
    а не 404. Оболочка не содержит данных тенанта — их отдают /api/*,
    закрытые require_user; сам SPA уводит анонима на /login."""
    resp = await anon_client.get(path)
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    assert "text/html" in resp.headers.get("content-type", "")
