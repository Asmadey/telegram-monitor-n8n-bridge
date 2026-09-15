"""Сведение не уложилось в потолок — и в бота ушёл сырой JSON (находка владельца).

Через час после лечения склейки промптов (`test_142`) владелец снова получил
JSON — на этот раз от источника из ВОСЬМИ каналов, где склейки быть не могло.
Формат сообщения назвал причину сам: «канал: вердикт», через пустую строку —
это запасной путь, который отдаёт сырые вердикты, когда оформление не
удалось. Журнал прода подтвердил дословно:

    AI_ERROR | ТОП-вакансии | Сведение не удалось:
              StepTimeout: тайм-аут 120 с: сведение по каналам

**Причина — один потолок на два разных шага.** `LLM_TIMEOUT = 120 с` писался
под разбор канала: тот читает посты и отвечает данными. Сведение ПИШЕТ
итоговое сообщение, и его длина растёт с числом находок, а не с числом
постов. У источника набралось около двадцати вакансий, генерация ответа не
уложилась в потолок, и запасной путь сработал с первой же попытки — хотя
вердикты каналов уже сохранены в задаче и повтор переспросил бы только
оформление.

Два правила, которые здесь закрепляются:

1. **У сведения свой потолок**, и он больше. Но не любой: отметка о жизни
   воркера ставится МЕЖДУ единицами работы, поэтому вызов длиннее окна
   устаревания сделал бы живого воркера мёртвым на вид. Потолок сведения
   выводится из этого окна, а не назначается на глаз.
2. **Отказ оформления восстановим — значит, сначала повтор.** Сырые вердикты
   остаются последним словом, а не первым.

И третье, про честность: если запасной путь всё-таки сработал, сообщение
обязано об этом СКАЗАТЬ. Молчаливая подмена оформленного ответа сырым — это
та же беда, что «совпадений нет» против «не смогли посмотреть».
"""

import json

import pytest
from test_58_worker_body import RecordingDispatcher, _worker
from test_112_map_reduce import THREE, ChannelTelegram, RecordingLLM, _source

from app.worker import (
    LLM_TIMEOUT,
    MAX_BATCH_ATTEMPTS,
    REDUCE_TIMEOUT,
    StepTimeout,
)

pytestmark = pytest.mark.asyncio

EXTRACT_A, EXTRACT_B = "искать первое", "искать второе"
FORMAT = "оформить в HTML"
SLOW = StepTimeout("тайм-аут 120 с: сведение по каналам")


class _Job:
    """Задача с известным числом попыток: повтор — часть контракта."""

    def __init__(self, attempts: int):
        self.attempts = attempts
        self.payload_json = ""


def _payload() -> dict:
    """Батч, уже разобранный по каналам: вердикты сохранены прошлой попыткой.

    Именно в таком виде задача и возвращается в очередь — поэтому повтор
    переспрашивает только оформление, а не каналы.
    """
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
                    "verdict": '[{"title": "А"}]',
                    "messages": [{"id": 11, "text": "пост"}],
                },
                {
                    "chat_id": -1002,
                    "chat_title": "beta",
                    "extract_prompt": EXTRACT_B,
                    "verdict": '[{"title": "Б"}]',
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


# --------------------------------------------------------------------------
# Потолки
# --------------------------------------------------------------------------


def test_the_reduce_gets_more_time_than_a_channel():
    assert REDUCE_TIMEOUT > LLM_TIMEOUT, (
        "у сведения тот же потолок, что у разбора канала, — а шаги разные: "
        "разбор читает посты, сведение пишет ответ, и его длина растёт с "
        "числом находок"
    )


def test_the_reduce_never_outlives_the_heartbeat_window():
    """Потолок выводится из окна устаревания, а не назначается на глаз.

    Отметка о жизни ставится между единицами работы: вызов длиннее окна
    сделал бы работающего воркера мёртвым на вид, и тревога 13.3 соврала бы.
    """
    from app.services.ops import WORKER_STALE_AFTER

    assert REDUCE_TIMEOUT <= WORKER_STALE_AFTER, (
        f"сведение ({REDUCE_TIMEOUT} с) живёт дольше окна устаревания "
        f"({WORKER_STALE_AFTER} с) — воркер будет выглядеть мёртвым, пока работает"
    )


# --------------------------------------------------------------------------
# Повтор вместо сырых вердиктов
# --------------------------------------------------------------------------


async def test_a_slow_reduce_is_retried_before_anything_is_delivered(db, user):
    """Первый отказ оформления — повод повторить, а не сдаться."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: SLOW})

    with pytest.raises(StepTimeout):
        await _run_batch(db, user, llm, attempts=0)


async def test_the_retry_asks_only_for_the_formatting(db, user):
    """Антивакуум: повтор не переспрашивает каналы — за них уже заплачено."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: SLOW})

    with pytest.raises(StepTimeout):
        await _run_batch(db, user, llm, attempts=0)

    prompts = [call["prompt"] for call in llm.calls]
    assert prompts == [FORMAT], f"повтор пошёл не только за оформлением: {prompts}"


async def test_the_last_attempt_delivers_the_findings_rather_than_nothing(db, user):
    """Находки дороже оформления: на последней попытке уходит сырой вердикт."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: SLOW})

    dispatcher = await _run_batch(db, user, llm, attempts=MAX_BATCH_ATTEMPTS - 1)

    assert dispatcher.calls, "на последней попытке не доставлено ничего"
    analysis = dispatcher.calls[0]["analysis"]
    assert '"title": "А"' in analysis, f"находки потеряны: {analysis!r}"


async def test_the_raw_fallback_says_that_it_is_raw(db, user):
    """Молчаливая подмена оформленного ответа сырым — та же беда, что тишина.

    Человек, получивший JSON без объяснения, ищет ошибку в промпте: именно
    так и вышло 15 сентября.
    """
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: SLOW})

    dispatcher = await _run_batch(db, user, llm, attempts=MAX_BATCH_ATTEMPTS - 1)
    analysis = dispatcher.calls[0]["analysis"]

    assert "оформ" in analysis.lower(), (
        f"сообщение не говорит, что оформление не удалось: {analysis[:200]!r}"
    )
    assert analysis.lower().index("оформ") < analysis.index('"title"'), (
        "предупреждение стоит ПОСЛЕ находок — его прочитают, пролистав JSON"
    )


async def test_a_working_reduce_is_neither_retried_nor_marked(db, user):
    """Антивакуум: удачное оформление уходит как есть, без пометок и повторов."""
    await _source(db, user, channels=[("@alpha", -1001, 20, EXTRACT_A)])
    llm = RecordingLLM(answers={FORMAT: "<b>Вакансии</b>"})

    dispatcher = await _run_batch(db, user, llm, attempts=0)

    assert dispatcher.calls, "ничего не доставлено"
    assert dispatcher.calls[0]["analysis"] == "<b>Вакансии</b>", (
        f"к удачному оформлению что-то приписано: {dispatcher.calls[0]['analysis']!r}"
    )
    assert json.dumps(llm.calls, ensure_ascii=False).count(FORMAT) == 1, (
        "оформление запрошено дважды при удачном первом запросе"
    )
