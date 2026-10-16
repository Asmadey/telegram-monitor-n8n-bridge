"""Флажок «Применить HTML-разметку» у источника (задача 13.27).

Заказ владельца 2026-10-15 после сводки «БАЛИ», ушедшей в бот буквами. Правила
оформления для Telegram — какие теги можно, что закрывать, что экранировать —
каждый владелец переписывал в свой промпт сам и по-своему, а модель на длинном
ответе от них отступала. Флажок отдаёт эти правила сервису: включён — к промпту
ИТОГОВОГО шага дописывается блок про оформление, один на все источники.

Итоговый шаг — тот, чей ответ уходит в бот: сведение, а если его нет (один
канал без промпта оформления), — разбор канала. В режиме шаблона итог собирает
код, и модели оформлять нечего.

Проверяется ВХОД модели через настоящий конвейер, подменён только её вызов.
"""

import re
from pathlib import Path

import pytest
from conftest import act_as
from test_58_worker_body import _worker
from test_112_map_reduce import ChannelTelegram, _post, _source
from test_126_usage_by_channel import _enable_ai

from app.services.telegram_markup import PLAIN_TAGS

pytestmark = pytest.mark.asyncio

STATIC = Path(__file__).resolve().parent.parent / "static"
EXTRACT = "искать объявления"
FORMAT = "оформи итог"
MARKER = "правило сервиса"
ALLOWED_LINE = re.compile(r"(?m)^- Разрешены только теги: (.+)$")


class Recorder:
    def __init__(self):
        self.systems: list[str] = []

    async def __call__(self, payload):
        self.systems.append(payload["messages"][0]["content"])
        return "ответ модели", 10

    def step(self, needle: str) -> str:
        found = [s for s in self.systems if needle in s]
        assert len(found) == 1, f"шагов с «{needle}»: {len(found)} — {self.systems}"
        return found[0]


async def _run(db, user, *, html_markup, answer_prompt=FORMAT, assembly_mode="prompt"):
    await _enable_ai(db, user.id)
    source = await _source(
        db,
        user,
        channels=[("@alpha", -1001, 5, EXTRACT)],
        answer_prompt=answer_prompt,
        assembly_mode=assembly_mode,
    )
    source.html_markup = html_markup
    await db.commit()
    recorder = Recorder()
    telegram = ChannelTelegram({-1001: ("@alpha", [_post(11)])})
    await _worker(db, telegram=telegram).poll_source(db, source, llm_caller=recorder)
    return recorder


async def test_the_block_goes_to_the_reduce_step(db, user):
    recorder = await _run(db, user, html_markup=True)
    assert MARKER in recorder.step(FORMAT), (
        f"сведение — итоговый шаг, а правил оформления в нём нет: {recorder.systems}"
    )
    assert MARKER not in recorder.step(EXTRACT), (
        "правила оформления ушли в разбор канала, чей ответ в бот не попадает"
    )


async def test_without_reduce_the_block_goes_to_the_channel(db, user):
    """Один канал без промпта оформления: ответ разбора и есть итог."""
    recorder = await _run(db, user, html_markup=True, answer_prompt="")
    assert len(recorder.systems) == 1, f"шагов {len(recorder.systems)}, ждали один"
    assert MARKER in recorder.systems[0], (
        f"итог пишет разбор канала, а правил оформления в нём нет: {recorder.systems}"
    )


async def test_switched_off_the_prompts_are_untouched(db, user):
    recorder = await _run(db, user, html_markup=False)
    assert not any(MARKER in s for s in recorder.systems), (
        f"флажок выключен, а правила оформления дописаны: {recorder.systems}"
    )


async def test_the_template_mode_is_not_touched(db, user):
    """В шаблоне итог рисует код: правила HTML только спорили бы со схемой."""
    recorder = await _run(db, user, html_markup=True, assembly_mode="template")
    assert not any(MARKER in s for s in recorder.systems), (
        f"режим шаблона получил правила оформления: {recorder.systems}"
    )


async def test_the_block_names_only_tags_the_bot_api_takes(db, user):
    """Разрешённые теги — из того же списка, по которому чистит доставка.

    Блок, обещающий модели тег, которого `sendMessage` не знает, — это 400 на
    запасном пути и сообщение буквами, то есть ровно то, что флажок лечит.
    """
    system = (await _run(db, user, html_markup=True)).step(FORMAT)
    line = ALLOWED_LINE.search(system)
    assert line, f"в блоке нет строки с разрешёнными тегами: {system}"
    named = set(re.findall(r"<([a-z-]+)", line.group(1)))
    assert named and named <= PLAIN_TAGS, (
        f"блок разрешает теги, которых нет у sendMessage: {named - PLAIN_TAGS}"
    )


async def test_the_switch_is_saved_and_shown(anon_client, db, user):
    await act_as(anon_client, db, user)
    created = await anon_client.post(
        "/api/sources", json={"title": "Бали", "html_markup": True}
    )
    assert created.status_code == 201, created.text
    assert created.json().get("html_markup") is True, created.json()

    public_id = created.json()["public_id"]
    patched = await anon_client.patch(
        f"/api/sources/{public_id}", json={"html_markup": False}
    )
    assert patched.json().get("html_markup") is False, patched.json()
    listed = (await anon_client.get("/api/sources")).json()["sources"]
    assert [s.get("html_markup") for s in listed] == [False], listed


async def test_a_new_source_starts_switched_off(anon_client, db, user):
    await act_as(anon_client, db, user)
    created = await anon_client.post("/api/sources", json={"title": "Бали"})
    assert created.json().get("html_markup") is False, (
        f"флажок включён без спроса — промпты владельца поменялись бы сами: "
        f"{created.json()}"
    )


def test_both_source_windows_have_the_switch():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "sources.js").read_text(encoding="utf-8")
    for element in ("sourceHtmlMarkup", "editSourceHtmlMarkup"):
        assert f'id="{element}"' in html, f"нет флажка {element} в окне источника"
        assert element in js, f"флажок {element} не читается и не заполняется"
    assert "Применить HTML-разметку" in html
    assert "html_markup" in js, "флажок не уходит на сервер"
