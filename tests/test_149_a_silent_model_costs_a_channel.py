"""Молчание модели на разборе канала стоило каналу постов (задача 13.11).

Найдено владельцем 16 сентября: «ему не удалось разобрать сообщения из
источника „Job for Products and Projects“». Журнал прода:

    AI_ERROR | Job for Products and Projects | Разбор канала не удался:
              StepTimeout: тайм-аут 120 с: разбор канала

и сутками раньше — то же самое на «Finder.work». Разные источники, разные
каналы: это не один больной канал, а обычная медлительность провайдера.

**Асимметрия, которой не должно быть.** Сбой провайдера (`RuntimeError`)
возвращает батч в очередь и повторяется до трёх раз. Тайм-аут — подкласс
`TimeoutError`, в ветку `RuntimeError` он не попадает и уходит в общий
`except Exception`, где повтора нет вовсе: канал с первой же попытки
помечается неразобранным. При этом молчание модели восстановимо не меньше,
чем 502 от провайдера, — скорее больше.

**Цена ошибки — не оформление, а посты.** Комментарий в самом воркере
говорит прямо: посты канала уже зарезервированы дедупликацией, и «пометить
канал неразобранным и пойти дальше — значит потерять их навсегда». То есть
один медленный ответ модели навсегда съедал сообщения канала за прогон, и
человек узнавал об этом строкой «⚠️ Не разобраны: …» в конце сводки.

Задача 13.9 (#88) закрепила это же правило на шаге СВЕДЕНИЯ: «отказ
восстановим — значит, сначала повтор, а сырой вердикт последнее слово».
Здесь то же правило распространяется на шаг РАЗБОРА.

Что НЕ повторяется и почему: исчерпанный месячный бюджет и отсутствие ключа
— это про весь источник, а не про канал. Следующая попытка упрётся в тот же
потолок, поэтому задача откладывается целиком, а не крутится впустую.
"""

import pytest
from test_58_worker_body import RecordingDispatcher, _worker
from test_112_map_reduce import THREE, ChannelTelegram, RecordingLLM, _source

from app.services.llm import MonthlyTokenBudgetExhausted
from app.worker import MAX_BATCH_ATTEMPTS, StepTimeout, _recoverable

EXTRACT_A, EXTRACT_B = "искать первое", "искать второе"
FORMAT = "оформить в HTML"
SLOW = StepTimeout("тайм-аут 120 с: разбор канала alpha")


# --------------------------------------------------------------------------
# Правило повтора — без базы, поэтому проверяется везде
# --------------------------------------------------------------------------


def test_a_silent_model_is_worth_a_retry():
    """Главное утверждение: тайм-аут разбора восстановим."""
    assert _recoverable(SLOW), (
        "молчание модели считается окончательным — канал теряет посты "
        "с первой же попытки"
    )


def test_a_provider_failure_stays_recoverable():
    """Антивакуум: правило добавляется к прежнему, а не заменяет его."""
    assert _recoverable(RuntimeError("502 Bad Gateway")), (
        "сбой провайдера перестал быть поводом для повтора"
    )


def test_an_exhausted_budget_is_not_a_channel_problem():
    """Бюджет — про весь источник: повтор упрётся в тот же потолок."""
    assert not _recoverable(MonthlyTokenBudgetExhausted("лимит")), (
        "исчерпанный бюджет уйдёт на три круга впустую"
    )


def test_a_missing_key_is_not_a_channel_problem():
    """Ключа нет у источника, а не у канала — повторять нечего."""
    assert not _recoverable(RuntimeError("LLM enabled without API key")), (
        "источник без ключа будет перезапрашиваться до исчерпания попыток"
    )


def test_a_broken_answer_is_not_retried():
    """Не всякий отказ восстановим: испорченные данные повтор не вылечит."""
    assert not _recoverable(ValueError("сломанный JSON")), (
        "повторяется всё подряд — попытки тратятся на то, что не исправится"
    )


# --------------------------------------------------------------------------
# Поведение конвейера на настоящем батче
# --------------------------------------------------------------------------


class _Job:
    def __init__(self, attempts: int):
        self.attempts = attempts
        self.payload_json = ""


def _payload() -> dict:
    """Батч ДО разбора: вердиктов ещё нет, каналы будут спрошены сейчас."""
    return {
        "batch": {
            "job_id": "job-1",
            "source_public_id": "src-1",
            "chat_title": "ТОП-вакансии",
            "messages_count": 2,
            "channels": [
                {
                    "chat_id": -1001,
                    "chat_title": "alpha",
                    "extract_prompt": EXTRACT_A,
                    "messages": [{"id": 11, "text": "пост"}],
                },
                {
                    "chat_id": -1002,
                    "chat_title": "beta",
                    "extract_prompt": EXTRACT_B,
                    "messages": [{"id": 21, "text": "пост"}],
                },
            ],
            "unparsed": [],
        },
        "answer_prompt": FORMAT,
    }


async def _run_batch(db, user, llm, *, attempts: int):
    dispatcher = RecordingDispatcher()
    worker = _worker(db, telegram=ChannelTelegram(THREE), dispatcher=dispatcher)
    worker.llm = llm
    await worker._run_source_batch(db, user.id, _payload(), job=_Job(attempts))
    return dispatcher


@pytest.mark.asyncio
async def test_a_slow_channel_returns_the_batch_to_the_queue(db, user):
    """Первое молчание — повод повторить, а не хоронить посты канала."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={EXTRACT_A: SLOW})

    with pytest.raises(StepTimeout):
        await _run_batch(db, user, llm, attempts=0)


@pytest.mark.asyncio
async def test_the_last_attempt_still_delivers_the_other_channels(db, user):
    """Повтор не отменяет прежнего: на последней попытке сводка уходит.

    Канал назван неразобранным — молчать о потере нельзя, — но семь
    остальных каналов доезжают до человека.
    """
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={EXTRACT_A: SLOW}, default="находка")

    dispatcher = await _run_batch(db, user, llm, attempts=MAX_BATCH_ATTEMPTS - 1)

    assert dispatcher.calls, "на последней попытке не доставлено ничего"
    assert "alpha" in dispatcher.calls[0]["analysis"], (
        f"канал потерян молча: {dispatcher.calls[0]['analysis'][:200]!r}"
    )


@pytest.mark.asyncio
async def test_a_working_channel_is_neither_retried_nor_marked(db, user):
    """Антивакуум: обычный прогон не должен ни повторяться, ни пугать пометкой."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: "<b>Вакансии</b>"})

    dispatcher = await _run_batch(db, user, llm, attempts=0)

    assert dispatcher.calls, "ничего не доставлено"
    assert "Не разобраны" not in dispatcher.calls[0]["analysis"], (
        f"к удачному прогону приписана пометка: {dispatcher.calls[0]['analysis']!r}"
    )
