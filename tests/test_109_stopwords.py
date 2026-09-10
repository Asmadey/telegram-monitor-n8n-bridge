"""Стоп-слова: отсев до обращения к модели (задача 11.3).

Решение владельца: «я не хочу получать вакансии для разработчиков» —
ставлю «разработчик» стоп-словом, и такие посты до LLM не доходят вовсе.
Токены не жгутся, а модели не приходится исключать то, что можно было
отсеять строкой.

Два правила, без которых отсев превращается в тихую потерю постов:

1. **Отсеянный пост остаётся виденным.** Он уже зарезервирован в
   `sent_messages`, и иначе вернулся бы на следующем прогоне — то есть
   отсеивался бы вечно, каждый раз заново.
2. **Отсеянное всегда посчитано.** Пользователь должен видеть, что из
   сорока постов до модели дошло три: без счётчика слишком широкое
   стоп-слово выглядит как «канал замолчал».

Совпадение ищется подстрокой без учёта регистра — и это осознанный
компромисс. «разработчик» отсечёт «разработчица» (обычно то, что нужно),
но отсечёт и пост, где слово попалось случайно. Поэтому счётчик
обязателен, а в интерфейсе (11.7) будет предпросмотр.

Ищем ТОЛЬКО в тексте поста. Ссылка на пост несёт имя канала, и стоп-слово,
совпавшее с ним, вырезало бы канал целиком — молча и необъяснимо.
"""

import pytest

from app.services.stopwords import parse_stop_words, split_by_stop_words


def _post(msg_id: int, text: str, url: str = "") -> dict:
    return {"id": msg_id, "text": text, "post_url": url or f"https://t.me/c/{msg_id}"}


def test_parse_takes_one_word_per_line():
    words = parse_stop_words("разработчик\n  Тестировщик  \n\ndevops\n")
    assert words == ["разработчик", "тестировщик", "devops"], (
        f"разбор списка потерял или исказил слова: {words}"
    )


def test_parse_drops_duplicates_and_empty_input():
    assert parse_stop_words("") == []
    assert parse_stop_words("   \n\n  ") == []
    assert parse_stop_words("qa\nQA\n qa ") == ["qa"]


def test_parse_has_a_ceiling():
    """Потолок — не придирка: каждое слово это проход по каждому посту."""
    words = parse_stop_words("\n".join(f"слово{i}" for i in range(500)))
    assert len(words) <= 100, f"список стоп-слов не ограничен: {len(words)}"


def test_empty_list_filters_nothing():
    posts = [_post(1, "любой текст"), _post(2, "другой")]
    kept, dropped = split_by_stop_words(posts, [])
    assert kept == posts and dropped == []


def test_case_is_ignored():
    posts = [_post(1, "Ищем РАЗРАБОТЧИКА в команду")]
    kept, dropped = split_by_stop_words(posts, ["разработчик"])
    assert kept == [] and len(dropped) == 1, (
        "совпадение зависит от регистра — пользователю пришлось бы "
        "перечислять все написания"
    )


def test_only_matching_posts_are_dropped():
    posts = [
        _post(1, "Нужен разработчик на Python"),
        _post(2, "Ищем менеджера по продажам"),
        _post(3, "Требуется QA-инженер"),
    ]
    kept, dropped = split_by_stop_words(posts, ["разработчик", "qa"])

    assert [m["id"] for m in kept] == [2], f"отсеяно лишнее: {[m['id'] for m in kept]}"
    assert [m["id"] for m in dropped] == [1, 3]


def test_link_is_not_searched():
    """Стоп-слово в ссылке вырезало бы канал целиком — молча."""
    posts = [_post(1, "Менеджер по продажам", url="https://t.me/devjobs/1")]
    kept, dropped = split_by_stop_words(posts, ["dev"])
    assert kept and not dropped, "совпадение найдено в ссылке, а не в тексте"


def test_post_without_text_survives():
    """Пост без текста (картинка, файл) отсеять по слову нельзя."""
    kept, dropped = split_by_stop_words([{"id": 1, "text": None}], ["разработчик"])
    assert len(kept) == 1 and dropped == []


# --------------------------------------------------------------------------
# Отсев на пути опроса
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filtered_posts_never_reach_the_batch(db, user):
    """До модели доходит только то, что прошло отсев."""
    import json

    from sqlalchemy import select
    from test_58_worker_body import FakeTelegram, _monitor, _utc, _worker

    from app.models import Job

    posts = [
        _post(11, "Нужен разработчик на Python"),
        _post(12, "Ищем менеджера по продажам"),
    ]
    await _monitor(db, user, last_checked=_utc(hours=3), stop_words="разработчик")
    worker = _worker(db, telegram=FakeTelegram(posts=posts))

    await worker.run_schedule(db)

    job = (await db.scalars(select(Job).where(Job.kind == "process_batch"))).first()
    assert job is not None, "батч не создан вовсе"
    payload = json.loads(job.payload_json)
    # батч источника несёт каналы, а не плоский список (11.4)
    ids = [m["id"] for g in payload["batch"]["channels"] for m in g["messages"]]
    assert ids == [12], f"в модель ушёл отсеянный пост: {ids}"


@pytest.mark.asyncio
async def test_filtered_post_stays_seen(db, user):
    """Иначе отсев повторялся бы вечно, на каждом прогоне заново."""
    from sqlalchemy import select
    from test_58_worker_body import FakeTelegram, _monitor, _utc, _worker

    from app.models import SentMessage

    await _monitor(db, user, last_checked=_utc(hours=3), stop_words="разработчик")
    worker = _worker(db, telegram=FakeTelegram(posts=[_post(11, "Нужен разработчик")]))

    await worker.run_schedule(db)

    stored = list(await db.scalars(select(SentMessage.message_id)))
    assert stored == [11], f"отсеянный пост не записан как виденный: {stored}"


@pytest.mark.asyncio
async def test_everything_filtered_is_reported_not_silent(db, user):
    """Все посты отсеяны — это не «новых нет», и в журнале это видно."""
    from sqlalchemy import select
    from test_58_worker_body import FakeTelegram, _monitor, _utc, _worker

    from app.models import LogEntry

    await _monitor(db, user, last_checked=_utc(hours=3), stop_words="разработчик")
    worker = _worker(
        db,
        telegram=FakeTelegram(
            posts=[
                _post(11, "Нужен разработчик"),
                _post(12, "Ищем DevOps-разработчика"),
            ]
        ),
    )

    await worker.run_schedule(db)

    entries = list(await db.scalars(select(LogEntry)))
    assert entries, "прогон не оставил следа"
    text = " ".join(e.details or "" for e in entries)
    assert "стоп-слов" in text.lower(), (
        f"отсев не назван в журнале: {text!r} — слишком широкое стоп-слово "
        "будет выглядеть как «канал замолчал»"
    )
    assert "2" in text, "не сказано, сколько постов отсеяно"


@pytest.mark.asyncio
async def test_count_of_filtered_posts_travels_with_the_batch(db, user):
    """Счётчик нужен и в карточке ленты, а не только в журнале."""
    import json

    from sqlalchemy import select
    from test_58_worker_body import FakeTelegram, _monitor, _utc, _worker

    from app.models import Job

    await _monitor(db, user, last_checked=_utc(hours=3), stop_words="разработчик")
    worker = _worker(
        db,
        telegram=FakeTelegram(
            posts=[_post(11, "Нужен разработчик"), _post(12, "Менеджер по продажам")]
        ),
    )

    await worker.run_schedule(db)

    job = (await db.scalars(select(Job).where(Job.kind == "process_batch"))).first()
    payload = json.loads(job.payload_json)
    counts = [g.get("filtered_count") for g in payload["batch"]["channels"]]
    assert counts == [1], f"счётчик отсеянных не доехал до батча: {counts}"
