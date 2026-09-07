"""Затирание секретов во ВСЕХ логах процесса (задача 9.4).

Журнал тенанта закрыт с 4.6: единственная точка записи `add_log` прогоняет
`details` через `redact`. У процесса есть второй выход наружу — stdout и
stderr, куда пишут `logging`, uvicorn, telethon и необработанные исключения.
Там затирания не было: `redact(str(exc))` в отдельных местах покрывает ровно
те строки, где о нём вспомнили, и не покрывает ни `exc_info=True`, ни
трейсбек упавшего запроса, ни лог сторонней библиотеки.

Поэтому затирание ставится не на вызовы, а на КАНАЛ — фабрику записей
logging. Через неё проходит каждая запись, созданная где угодно в процессе,
включая библиотеки, которые о нас не знают. Забыть вызвать `redact` на новом
месте больше нельзя: это тот же приём, что `TenantRepo` для user_id и
`add_log` для журнала.

Фильтр на обработчике сюда не годится: обработчики uvicorn создаются в свой
момент, и правило, повешенное на существующие, не попадёт на добавленные
позже.
"""

import logging
import traceback
from collections.abc import Callable

from app.services.journal import redact

RecordFactory = Callable[..., logging.LogRecord]

_installed: RecordFactory | None = None
_original: RecordFactory | None = None


def _redacting_factory(factory: RecordFactory) -> RecordFactory:
    def make_record(*args, **kwargs) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        # Секрет приезжает и в шаблоне, и в аргументах: logger.warning("%s", exc).
        # Затирается содержимое, СТРУКТУРА сохраняется: склеивание через
        # getMessage() с обнулением args ломало access-логгер uvicorn, который
        # получает пятёрку (адрес, метод, путь, версия, код) и раскладывает её
        # сам — на каждый запрос в лог печатался трейсбек. Найдено на живом
        # деплое 2026-09-07; ответы при этом отдавались верно, поэтому ни
        # тесты, ни healthcheck дефекта не показывали.
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(item) if isinstance(item, str) else item for item in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        # Трейсбек форматируется здесь и подставляется готовой строкой:
        # Formatter возьмёт exc_text как есть и второй раз форматировать
        # исключение не станет.
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )
            record.exc_info = None
        return record

    return make_record


def install_log_redaction() -> None:
    """Включить затирание для всего процесса. Идемпотентно."""
    global _installed, _original
    if _installed is not None and logging.getLogRecordFactory() is _installed:
        return
    _original = logging.getLogRecordFactory()
    _installed = _redacting_factory(_original)
    logging.setLogRecordFactory(_installed)


def reset_log_redaction() -> None:
    """Вернуть прежнюю фабрику. Нужно тестам, не рантайму."""
    global _installed, _original
    if _original is not None:
        logging.setLogRecordFactory(_original)
    _installed = None
    _original = None
