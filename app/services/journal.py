"""Журнал с затиранием секретов (задача 4.6 PLAN.md).

add_log оригинала (server.py:317) писал СЫРЫЕ тексты исключений.
HTTP-исключения httpx несут заголовки, в заголовках —
`Authorization: Bearer sk-or-...`; тексты исключений Telethon — строку
MTProto-сессии. Журнал читается в UI, экспортируется, попадает в
скриншоты — секрет в details = секрет скомпрометирован (а MTProto-
сессию нельзя сбросить удалённо).

ЕДИНСТВЕННАЯ точка записи в журнал — add_log: redact(details) до
записи. Прямой конструктор LogEntry вне journal.py — обход redact;
структурный свип в test_45 держит это навсегда: новый писатель журнала
(роутеры Фазы 5, воркер) обязан идти через add_log.

Паттерны (план 4.6): sk-or-[\w-]+ (OpenRouter), \d{8,10}:[\w-]{35}
(StringSession Telethon: номер + 35 url-safe символов), Bearer \S+.
Список расширяемый: новый секрет = новый паттерн + новый параметр
теста.
"""

import datetime
import re

from app.models import LogEntry

REDACTED = "[REDACTED]"

_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"sk-or-[\w-]+",
        r"\d{8,10}:[\w-]{35}",
        r"Bearer\s+\S+",
    )
)


def redact(text: str) -> str:
    """Затереть известные формы секретов; окружающий текст сохраняется
    (диагностика без секрета ценна, пустая — нет)."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


async def add_log(
    db,
    user_id: int,
    event_type: str,
    details: str,
    status: str = "INFO",
    *,
    chat_id: int | None = None,
    chat_title: str | None = None,
    messages_count: int = 0,
) -> LogEntry:
    """Записать событие в журнал тенанта; details затирается redact."""
    entry = LogEntry(
        user_id=user_id,
        event_type=event_type,
        details=redact(str(details or "")),
        status=status,
        chat_id=chat_id,
        chat_title=chat_title,
        messages_count=messages_count,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(entry)
    await db.commit()
    return entry
