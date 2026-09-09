"""Два дефекта, найденных владельцем 9 сентября (задача 9.18).

**1. Кнопка «Редактировать» ничего не открывает.** Обработчик находится,
идентификатор верный (это чинила 9.10), поля модалки заполняются — а сама
модалка так и не показывается: `openEditModal` не вызывает
`openModalAnimated`. Снаружи это выглядит как «кнопка не нажимается», и
отличить «обработчик не сработал» от «сработал и ничего не показал»
пользователю нечем.

Проверка написана свипом, а не точечно: модалка, которую умеют закрывать,
но не умеют открывать, — общий класс ошибки, и ловить его надо один раз
для всех.

**2. «Обновить анализ» переспрашивает модель БЕЗ промпта канала.**
Плановый разбор передаёт `custom_prompt` (worker.py:312), переразбор —
нет. У Finder.work промпт на 4311 символов; переразбор его карточки
возвращал ответ, собранный по умолчанию, то есть совсем другой. Выглядит
это как «модель стала хуже отвечать».

Заодно проверено то, о чём спросил владелец: переразбор НИЧЕГО не
отправляет — ни в бота, ни в n8n. Он только обновляет запись в ленте.
Это правильное поведение, и тест закрепляет его, чтобы доставка не
появилась здесь по недосмотру: нажатие «обновить» не должно рассылать
людям второе сообщение об одном и том же.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"


def test_every_modal_that_can_be_closed_can_be_opened():
    """Модалка, которую умеют только закрывать, недостижима.

    Первая версия свипа искала только `openModalAnimated` и обвинила
    модалку выбора диалогов: она открывается прямым
    `classList.add('active')` — то есть работает, просто без анимации.
    Ошибка была в тесте, и исправлена в тесте: контракт здесь «модалку
    можно открыть», а не «открыть определённым помощником».
    """
    closers: dict[str, str] = {}
    openers: set[str] = set()
    for path in sorted(JS_DIR.rglob("*.js")):
        if "vendor" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for name in re.findall(r"closeModalAnimated\(\s*(\w+)\s*\)", source):
            closers.setdefault(name, path.name)
        openers.update(re.findall(r"openModalAnimated\(\s*(\w+)\s*\)", source))
        openers.update(
            re.findall(r"(\w+)\.classList\.add\(\s*['\"]active['\"]\s*\)", source)
        )

    unreachable = {
        name: where for name, where in closers.items() if name not in openers
    }
    assert not unreachable, (
        "модалку умеют закрывать, но не открывать — снаружи это выглядит "
        f"как «кнопка не нажимается»: {unreachable}"
    )


def test_edit_handler_opens_the_modal():
    """Точечно — чтобы падение читалось без разбора свипа."""
    source = (JS_DIR / "channels.js").read_text(encoding="utf-8")
    body = source[source.index("function openEditModal") :]
    body = body[: body.index("\n}\n")]
    assert "openModalAnimated" in body, (
        f"openEditModal заполняет поля и не показывает модалку: {body[-200:]!r}"
    )


@pytest.mark.asyncio
async def test_reanalysis_uses_the_channel_prompt(db, user):
    """Переразбор обязан спрашивать модель тем же промптом, что и плановый."""
    import json

    from test_58_worker_body import _monitor, _worker

    from app.models import FeedItem

    monitor = await _monitor(db, user, prompt="искать только вакансии продаж")
    item = FeedItem(
        user_id=user.id,
        monitor_id=monitor.id,
        job_id="feed-1",
        chat_id=monitor.chat_id,
        raw_messages_json=json.dumps([{"id": 11, "text": "пост"}]),
    )
    db.add(item)
    await db.commit()

    seen: dict = {}

    async def fake_llm(db_, user_id, messages, **kwargs):
        seen.update(kwargs)
        return "новый анализ"

    worker = _worker(db)
    worker.llm = fake_llm

    await worker._reanalyze(db, user.id, item.id)

    assert seen.get("custom_prompt") == "искать только вакансии продаж", (
        "переразбор спросил модель без промпта канала — ответ будет собран "
        f"по умолчанию и не совпадёт с плановым: {seen}"
    )


@pytest.mark.asyncio
async def test_reanalysis_delivers_nothing(db, user):
    """Нажатие «обновить» не рассылает второе сообщение об одном и том же."""
    import json

    from test_58_worker_body import RecordingDispatcher, _monitor, _worker

    from app.models import FeedItem

    monitor = await _monitor(db, user)
    item = FeedItem(
        user_id=user.id,
        monitor_id=monitor.id,
        job_id="feed-2",
        chat_id=monitor.chat_id,
        raw_messages_json=json.dumps([{"id": 11, "text": "пост"}]),
    )
    db.add(item)
    await db.commit()

    dispatcher = RecordingDispatcher()
    worker = _worker(db, dispatcher=dispatcher)

    async def fake_llm(db_, user_id, messages, **kwargs):
        return "новый анализ"

    worker.llm = fake_llm
    await worker._reanalyze(db, user.id, item.id)

    assert dispatcher.calls == [], (
        "переразбор отправил доставку: пользователь получит второе "
        "сообщение об одном и том же батче"
    )
