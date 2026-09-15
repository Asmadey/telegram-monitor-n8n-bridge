"""Источник из одного канала отдавал сырой вердикт разбора (находка владельца).

В ленте и в боте у источника «Finder.work: работа и вакансии» приходил JSON
вместо оформленного сообщения, хотя оба промпта заполнены: у канала —
извлечение («Ответ должен содержать ТОЛЬКО JSON… форматированием занимается
другой промпт на следующем шаге»), у источника — оформление в HTML.

**Причина — оптимизация конвейера, а не промпт.** У источника один канал, и
разбор со сведением уходили ОДНИМ запросом: промпт канала и промпт источника
склеивались через пустую строку. Модель получала два взаимоисключающих набора
указаний разом и выполняла первый — «только JSON». Обещанного «следующего
шага» не существовало: результат единственного запроса и становился ответом.

Склейка срабатывала РОВНО в сломанном случае. `single and answer_prompt` —
это и есть «есть чем оформлять, но оформлять негде»: без промпта оформления
склеивать нечего, а с ним она ломает контракт, на который написаны оба
промпта.

Разбор и оформление — РАЗНЫЕ шаги, а не один шаг с двумя названиями. Первый
читает посты и отвечает данными, второй читает данные и отвечает сообщением
для человека. Число каналов на это не влияет: сводить при одном канале
действительно нечего, а оформлять — есть что.
"""

import pytest
from sqlalchemy import select
from test_58_worker_body import _worker
from test_112_map_reduce import THREE, ChannelTelegram, RecordingLLM, _run, _source

from app.models import FeedItem

pytestmark = pytest.mark.asyncio

EXTRACT = "верни только JSON, оформлением занимается следующий шаг"
FORMAT = "оформи находки в HTML для телеграма"
JSON_VERDICT = '[{"title": "Менеджер по продажам"}]'
HTML_ANSWER = "<b>Менеджер по продажам</b>"


async def _one_channel(db, user, *, answer_prompt: str):
    return await _source(
        db,
        user,
        channels=[("@alpha", -1001, 20, EXTRACT)],
        answer_prompt=answer_prompt,
    )


async def test_the_only_channel_still_goes_through_formatting(db, user):
    """Один канал — не повод пропускать оформление."""
    source = await _one_channel(db, user, answer_prompt=FORMAT)
    llm = RecordingLLM(answers={EXTRACT: JSON_VERDICT, FORMAT: HTML_ANSWER})
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    prompts = [call["prompt"] for call in llm.calls]
    assert len(llm.calls) == 2, (
        f"шагов {len(llm.calls)}, а промпта два — разбор и оформление: {prompts}"
    )
    assert FORMAT in prompts[-1], f"оформление не запускалось вовсе: {prompts}"


async def test_the_extraction_step_does_not_see_the_formatting_prompt(db, user):
    """Склейка двух промптов даёт модели взаимоисключающие указания.

    Проверяется не результат, а ВХОД: ответ модели на противоречивый ввод
    зависит от модели и завтра будет другим, а сам противоречивый ввод —
    дефект конвейера в любой день.
    """
    source = await _one_channel(db, user, answer_prompt=FORMAT)
    llm = RecordingLLM(answers={EXTRACT: JSON_VERDICT, FORMAT: HTML_ANSWER})
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    assert FORMAT not in llm.calls[0]["prompt"], (
        "промпт оформления подмешан в разбор: модель получила «верни только "
        f"JSON» и «оформи в HTML» одним запросом — {llm.calls[0]['prompt']!r}"
    )


async def test_the_card_carries_the_formatted_answer(db, user):
    """В ленту и в бота уходит оформленное сообщение, а не вердикт разбора.

    Здесь нужен НАСТОЯЩИЙ доставщик: двойник воркера по умолчанию карточку
    не пишет, и проверка зеленела бы, ничего не проверив.
    """
    from test_57_dispatch import Recorder, _enable_all

    from app.services.dispatch import dispatch as real_dispatch

    source = await _one_channel(db, user, answer_prompt=FORMAT)
    await _enable_all(db, user.id)
    llm = RecordingLLM(answers={EXTRACT: JSON_VERDICT, FORMAT: HTML_ANSWER})
    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=real_dispatch)
    worker.llm = llm

    await _run(db, source, worker, bot_sender=Recorder(result=True))

    card = (await db.scalars(select(FeedItem))).first()
    assert card is not None, "карточка в ленту не записана"
    assert card.ai_analysis == HTML_ANSWER, (
        f"в карточке сырой вердикт разбора вместо оформленного ответа: "
        f"{card.ai_analysis!r}"
    )


async def test_formatting_reads_the_verdict_and_not_the_posts_again(db, user):
    """Антивакуум: оформление не должно платить за посты второй раз.

    Второй шаг видит выжимку первого — это и делает схему map → reduce почти
    бесплатной по токенам. Если бы в него уезжали исходные посты, лечение
    стоило бы вдвое на каждом прогоне.
    """
    source = await _one_channel(db, user, answer_prompt=FORMAT)
    llm = RecordingLLM(answers={EXTRACT: JSON_VERDICT, FORMAT: HTML_ANSWER})
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    formatting_input = str(llm.calls[-1]["messages"])
    assert JSON_VERDICT in formatting_input, (
        f"оформление не получило вердикт разбора: {formatting_input[:200]}"
    )
    assert "пост" not in formatting_input, (
        f"в оформление уехали исходные посты — прогон платит за них дважды: "
        f"{formatting_input[:200]}"
    )


async def test_without_a_formatting_prompt_one_channel_still_costs_one_call(db, user):
    """Антивакуум: оформлять нечем — второй запрос не нужен.

    Лечение обязано чинить ровно склейку, а не вводить лишний запрос всем
    подряд: у источника без промпта оформления оформлять попросту нечего.
    """
    source = await _one_channel(db, user, answer_prompt="")
    llm = RecordingLLM(answers={EXTRACT: JSON_VERDICT})
    worker = _worker(db, telegram=ChannelTelegram(THREE))
    worker.llm = llm

    await _run(db, source, worker)

    assert len(llm.calls) == 1, (
        f"на один канал без оформления ушло {len(llm.calls)} запросов: "
        f"{[c['prompt'] for c in llm.calls]}"
    )
