"""Сведение получало разбор канала под видом поста (задача 13.24 `docs/PLAN.md`).

Найдено владельцем 2026-10-15 на источнике «БАЛИ»: один канал, промпт
оформления заполнен. Первая сводка начиналась с «в Telegram-канале есть один
пост-дайджест (ссылка: https://t.me/c/1). Конкретные авторы не указаны», вторая
— с «Вердикт: так отправить нельзя… что сломается в вашем посте».

**Причина — вход сведения, а не промпт.** Вердикты каналов шли в модель тем же
кодом, что посты Telegram: `{"post": [{"ID": "1", "пост": <весь разбор>,
"ссылка": "https://t.me/c/1"}]}`. Ссылка выдумана из порядкового номера,
название канала терялось. Модель честно описывала то, что ей подали: один пост
с несуществующей ссылкой. А когда разбор оказался HTML-документом, а промпт
оформления говорил про Telegram, модель отрецензировала «ваш пост».

Проверяется ВХОД модели, а не её ответ: ответ на путаный ввод зависит от
модели, а сам путаный ввод — дефект конвейера в любой день. Подменено только
обращение к модели (`llm_caller`); сборка запроса — настоящая.
"""

import json

import pytest
from test_58_worker_body import _worker
from test_112_map_reduce import ChannelTelegram, _post, _source
from test_126_usage_by_channel import _enable_ai

pytestmark = pytest.mark.asyncio

EXTRACT_A = "искать объявления А"
EXTRACT_B = "искать объявления Б"
FORMAT = "оформи итог для Telegram"
VERDICT = {EXTRACT_A: "разбор канала alpha", EXTRACT_B: "разбор канала beta"}


class Recorder:
    """Помнит каждый запрос к модели; отвечает вердиктом по промпту."""

    def __init__(self):
        self.payloads: list[dict] = []

    async def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        system = payload["messages"][0]["content"]
        for prompt, verdict in VERDICT.items():
            if prompt in system:
                return verdict, 10
        return "итоговое сообщение", 10

    def reduce(self) -> dict:
        found = [p for p in self.payloads if FORMAT in p["messages"][0]["content"]]
        assert len(found) == 1, f"запросов сведения {len(found)}, ждали один"
        return found[0]


async def _run(db, user, channels) -> Recorder:
    await _enable_ai(db, user.id)
    source = await _source(db, user, channels=channels, answer_prompt=FORMAT)
    posts = {-1001: ("@alpha", [_post(11)]), -1002: ("@beta", [_post(21)])}
    posts = {chat_id: posts[chat_id] for _, chat_id, *_ in channels}
    recorder = Recorder()
    await _worker(db, telegram=ChannelTelegram(posts)).poll_source(
        db, source, llm_caller=recorder
    )
    return recorder


ONE = [("@alpha", -1001, 5, EXTRACT_A)]
TWO = [("@alpha", -1001, 5, EXTRACT_A), ("@beta", -1002, 5, EXTRACT_B)]


async def test_the_reduce_step_gets_no_invented_post_link(db, user):
    """Ссылки `t.me/c/1` не существует: её собирали из номера вердикта."""
    content = (await _run(db, user, ONE)).reduce()["messages"][1]["content"]
    assert "t.me/c/" not in content and '"ссылка"' not in content, (
        f"сведению выдана ссылка на пост, которого у вердикта нет: {content}"
    )
    assert '"post"' not in content, (
        f"разбор канала подан модели как пост Telegram: {content}"
    )


async def test_the_reduce_step_sees_each_channel_by_name(db, user):
    """Сведение видит, ЧЕЙ это разбор, в порядке каналов источника."""
    content = (await _run(db, user, TWO)).reduce()["messages"][1]["content"]
    assert json.loads(content) == {
        "каналы": [
            {"канал": "alpha", "разбор": "разбор канала alpha"},
            {"канал": "beta", "разбор": "разбор канала beta"},
        ]
    }, f"вход сведения не по каналам: {content}"


async def test_the_service_tells_the_model_what_it_is_reading(db, user):
    """Промпт оформления пишет человек, и про устройство конвейера он не знает.

    Что перед моделью разбор, а не посты, говорит сервис — строкой перед
    промптом пользователя, а сам промпт остаётся целиком.
    """
    system = (await _run(db, user, ONE)).reduce()["messages"][0]["content"]
    assert "а не посты" in system, (
        f"модели не сказано, что перед ней результаты разбора: {system}"
    )
    assert system.endswith(FORMAT), f"промпт оформления потерян или обрезан: {system}"


async def test_the_extraction_step_still_reads_posts(db, user):
    """Разбор канала по-прежнему видит посты с их настоящими ссылками."""
    recorder = await _run(db, user, ONE)
    extract = [p for p in recorder.payloads if EXTRACT_A in p["messages"][0]["content"]]
    assert extract, "разбор канала не запускался"
    body = json.loads(extract[0]["messages"][1]["content"])
    assert body["post"][0]["ссылка"] == "https://t.me/c/11", (
        f"разбор канала потерял ссылку на пост: {body}"
    )
    assert "а не посты" not in extract[0]["messages"][0]["content"], (
        "служебная строка сведения попала в разбор канала"
    )
