"""Задача 9.4: секрет не должен уходить в стандартные логи.

Журнал тенанта закрыт с 4.6: единственная точка записи — `add_log`, она
затирает `details` через `redact`. Но у процесса есть второй выход наружу —
stdout/stderr, куда пишут `logging`, uvicorn и необработанные исключения.
Там затирания не было ни у кого: `redact(str(exc))` в отдельных вызовах
покрывает ровно те строки, где о нём вспомнили, и не покрывает ни
`exc_info=True`, ни трейсбек упавшего запроса, ни лог сторонней библиотеки.

Логи Railway читаются в браузере, копируются в переписку и попадают в
скриншоты. Утёкшая MTProto-сессия не отзывается удалённо — это цена
одного забытого вызова.

Поэтому проверка идёт не по вызовам, а по каналу: что бы и где бы ни
залогировали, на выходе секрета нет.
"""

import io
import logging

import pytest

# Синтетические секреты собираются в рантайме: скан не отличает выдуманный
# ключ от настоящего и справедливо ловит литерал (урок 4 сентября, test_53).
FAKE_OPENROUTER = "sk-or-" + "v1-" + "b" * 32
FAKE_BOT_TOKEN = "1234567890" + ":" + "c" * 35


@pytest.fixture
def captured_root_logs():
    """Свой обработчик на корневом логгере + установленное затирание."""
    from app.security.log_redaction import install_log_redaction, reset_log_redaction

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    install_log_redaction()
    try:
        yield buffer
    finally:
        reset_log_redaction()
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_secret_inside_a_traceback_never_reaches_stdout(captured_root_logs):
    """`exc_info=True` — самый частый способ отправить секрет наружу."""
    log = logging.getLogger("app.tests.hygiene")
    try:
        raise RuntimeError(
            f"OpenRouter отказал: Authorization: Bearer {FAKE_OPENROUTER}"
        )
    except RuntimeError:
        log.warning("доставка не удалась", exc_info=True)

    out = captured_root_logs.getvalue()
    assert FAKE_OPENROUTER not in out, "ключ ушёл в stdout внутри трейсбека"
    assert "[REDACTED]" in out, "затирание не сработало вовсе"
    assert "RuntimeError" in out, (
        "вместе с секретом потерян и диагноз — затирание не должно съедать трейсбек"
    )


def test_secret_passed_as_a_format_argument_is_redacted(captured_root_logs):
    """`logger.warning('%s', exc)` — секрет приезжает в args, а не в msg."""
    logging.getLogger("app.tests.hygiene").error("сессия: %s", FAKE_BOT_TOKEN)

    out = captured_root_logs.getvalue()
    assert FAKE_BOT_TOKEN not in out, "секрет ушёл в stdout через аргумент формата"
    assert "[REDACTED]" in out


def test_third_party_logger_is_covered_too(captured_root_logs):
    """Затирание висит на канале, а не на наших вызовах: логгер чужой
    библиотеки (telethon, httpx, uvicorn) проходит через ту же проверку."""
    logging.getLogger("telethon.network.mtprotosender").info(
        "auth key: %s", FAKE_BOT_TOKEN
    )

    assert FAKE_BOT_TOKEN not in captured_root_logs.getvalue()


def test_ordinary_message_survives_untouched(captured_root_logs):
    """Затирание не должно портить обычную диагностику."""
    logging.getLogger("app.tests.hygiene").info("тик воркера: задач %d", 7)

    assert "тик воркера: задач 7" in captured_root_logs.getvalue()


def test_install_is_idempotent():
    """Повторный вызов не должен наслаивать обёртки: два процесса (web и
    воркер) зовут установку каждый у себя, а тесты — ещё и по разу на тест."""
    from app.security.log_redaction import install_log_redaction, reset_log_redaction

    install_log_redaction()
    first = logging.getLogRecordFactory()
    install_log_redaction()
    assert logging.getLogRecordFactory() is first, "фабрика обёрнута дважды"
    reset_log_redaction()


def test_both_entry_points_install_redaction():
    """Трипваер: затирание бесполезно, если его забыли включить.

    Проверяется исходник обеих точек входа — web (`app/main.py`) и воркер
    (`app/worker.py`). Оба процесса пишут в один и тот же stdout Railway.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for entry in ("app/main.py", "app/worker.py"):
        source = (root / entry).read_text(encoding="utf-8")
        assert "install_log_redaction()" in source, (
            f"{entry} не включает затирание логов — секрет уйдёт в stdout"
        )


def test_structured_arguments_survive_for_formatters_that_need_them():
    """Затирание не имеет права ломать чужой форматтер.

    Найдено на живом деплое 2026-09-07: access-логгер uvicorn получает
    args пятёркой (адрес, метод, путь, версия, код) и раскладывает её сам.
    Первая версия затирания склеивала сообщение через getMessage() и
    обнуляла args — форматтер падал, и на КАЖДЫЙ запрос в лог печатался
    трейсбек `--- Logging error ---`. Ответы при этом отдавались верно, то
    есть тесты и healthcheck молчали: дефект видно только в логах.

    Контракт: структура args сохраняется, затирается содержимое строк.
    """
    from app.security.log_redaction import install_log_redaction, reset_log_redaction

    install_log_redaction()
    try:
        factory = logging.getLogRecordFactory()
        record = factory(
            "uvicorn.access",
            logging.INFO,
            "h11_impl.py",
            482,
            '%s - "%s %s HTTP/%s" %d',
            ("100.64.0.5:43194", "GET", "/api/telegram/status", "1.1", 401),
            None,
        )
        assert isinstance(record.args, tuple), (
            f"args перестали быть кортежем: {type(record.args).__name__}"
        )
        assert len(record.args) == 5, (
            f"форматтер uvicorn ждёт пять аргументов, получит {len(record.args)}"
        )
        assert record.getMessage().endswith("401")
    finally:
        reset_log_redaction()


def test_secret_inside_one_argument_is_still_redacted():
    """Сохранение структуры не должно стоить самой защиты."""
    from app.security.log_redaction import install_log_redaction, reset_log_redaction

    install_log_redaction()
    try:
        factory = logging.getLogRecordFactory()
        record = factory(
            "telethon",
            logging.INFO,
            "x.py",
            1,
            "auth: %s (%d)",
            (FAKE_BOT_TOKEN, 7),
            None,
        )
        assert FAKE_BOT_TOKEN not in record.getMessage()
        assert record.args[1] == 7, "нестроковый аргумент испорчен затиранием"
    finally:
        reset_log_redaction()
