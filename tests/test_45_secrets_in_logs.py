"""Задача 4.6 — секреты не попадают в журнал.

add_log оригинала (server.py:317) пишет СЫРЫЕ тексты исключений.
HTTP-исключения httpx несут заголовки, в заголовках —
`Authorization: Bearer sk-or-...`; тексты исключений Telethon — строку
MTProto-сессии. Журнал читается в UI, экспортируется, попадает в
скриншоты — секрет в details = секрет скомпрометирован.

Единственная точка записи в журнал — `add_log` (app/services/journal.py):
redact(details) до записи. Прямой конструктор LogEntry вне journal.py —
обход redact; структурный свип держит это навсегда.
"""

import datetime
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import LogEntry
from app.services.journal import add_log, redact

# StringSession Telethon: 8-10 цифр, двоеточие, 35 url-safe символов
SESSION = "79991234567:" + "AbCdEf-_0123456789AbCdEf-_0123456789AbCd"


def test_redact_strips_known_secret_shapes():
    """Все паттерны плана: OpenRouter-ключ, MTProto-сессия, Bearer —
    заменяются, окружающий текст сохраняется."""
    text = (
        "Ошибка запроса Authorization: Bearer sk-or-v1-secretkey при "
        f"сессии {SESSION} и ключе sk-or-v1-plain"
    )
    cleaned = redact(text)
    assert "sk-or-v1-secretkey" not in cleaned, "OpenRouter-ключ утёк в журнал"
    assert "sk-or-v1-plain" not in cleaned, "отдельный ключ тоже утёк"
    assert SESSION not in cleaned, "строка MTProto-сессии утекла в журнал"
    assert "Ошибка запроса" in cleaned, "redact уничтожил не секрет, а весь текст"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-or-v1-secretkey",
        "Bearer sk-or-v1-secretkey",
        SESSION,
        f"сессия клиента: {SESSION}",
    ],
)
def test_redact_leaves_no_secret(secret):
    """Секрет в любом окружении не выживает redact."""
    assert secret not in redact(f"контекст: {secret} хвост")


@pytest.mark.asyncio
async def test_add_log_redacts_details(db, user_a):
    """ПЛАН задачи: add_log(details='Bearer sk-or-v1-secret') сохраняет
    строку БЕЗ секрета — и с осмысленным окружением."""
    entry = await add_log(
        db,
        user_a.id,
        "OPENROUTER_ERROR",
        "401: заголовок Authorization: Bearer sk-or-v1-secret отклонён",
        status="ERROR",
    )
    stored = await db.get(LogEntry, entry.id)
    assert "sk-or-v1-secret" not in (stored.details or ""), (
        "секрет сохранён в журнале в открытом виде"
    )
    assert "401" in (stored.details or "")


@pytest.mark.asyncio
async def test_add_log_writes_scoped_entry(db, user_a, user_b):
    """Запись журнала — тенантная: user_id, event_type, status, счётчик
    сообщений на месте; чужой тенант её не видит (фильтр — дело роу-
    тера, но user_id в строке обязателен)."""
    before = datetime.datetime.now(datetime.timezone.utc)
    entry = await add_log(
        db,
        user_a.id,
        "POLL_OK",
        "5 новых сообщений",
        status="OK",
        chat_id=-100123,
        chat_title="Канал",
        messages_count=5,
    )
    stored = await db.get(LogEntry, entry.id)
    assert stored.user_id == user_a.id
    assert stored.event_type == "POLL_OK"
    assert stored.status == "OK"
    assert stored.messages_count == 5
    assert stored.chat_id == -100123
    assert stored.details == "5 новых сообщений"
    # sqlite отдаёт naive, свежий объект — aware: сравниваем оба без tz
    assert stored.timestamp.replace(tzinfo=None) >= before.replace(tzinfo=None)

    own = (
        await db.execute(select(LogEntry).where(LogEntry.user_id == user_a.id))
    ).scalars()
    assert list(own) == [stored]


@pytest.mark.asyncio
async def test_llm_error_path_redacts_secret(db, _env, user_a):
    """СЦЕНАРИЙ УТЕЧКИ: API-вызов падает с секретом в тексте исключения
    (httpx кладёт заголовки в repr) → llm.py пишет через add_log →
    в журнале секрета НЕТ, ошибка есть."""
    from app.services.integrations import save_integration_secrets
    from app.services.llm import process_messages_batch_with_llm

    await save_integration_secrets(db, user_a.id, openrouter_api_key="sk-or-live-key")
    from app.models import Integration

    integration = (
        await db.execute(select(Integration).where(Integration.user_id == user_a.id))
    ).scalar_one()
    integration.openrouter_enabled = True
    await db.commit()

    async def broken_caller(payload):
        raise RuntimeError(
            "401 Unauthorized: headers={'Authorization': 'Bearer sk-or-live-key'}"
        )

    result = await process_messages_batch_with_llm(
        db, user_a.id, [{"id": 1, "text": "пост"}], caller=broken_caller
    )
    assert result is None
    entries = list(
        (
            await db.execute(select(LogEntry).where(LogEntry.user_id == user_a.id))
        ).scalars()
    )
    assert entries, "ошибка API не записана в журнал вовсе"
    for entry in entries:
        assert "sk-or-live-key" not in (entry.details or ""), (
            "ключ OpenRouter утёк в журнал через текст исключения"
        )
    assert any("401" in (e.details or "") for e in entries), (
        "диагностика ошибки потеряна вместе с секретом"
    )


def test_only_journal_writes_log_entries():
    """Структурный свип: прямой конструктор LogEntry вне journal.py —
    обход redact. Каждый новый писатель журнала обязан идти через
    add_log."""
    violations = []
    for path in Path("app").rglob("*.py"):
        if path.name == "journal.py":
            continue
        # models/ ДЕКЛАРИРУЕТ класс (class LogEntry(Base)) — это не запись
        if "models" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"\bLogEntry\s*\(", source):
            violations.append(str(path))
    assert violations == [], f"LogEntry строится мимо add_log: {violations}"
