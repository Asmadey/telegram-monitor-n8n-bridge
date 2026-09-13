"""Каждый документ в корне назван в карте (задача 12.5 `docs/PLAN.md`).

Ревью 2026-09-10 назвало два файла в корне сиротами: `DESIGN-webflow.md` и
`Telegram_Parser_Telethon.md` — на них не ссылался ни один документ, и оба
уезжали в образ. Проверка 2026-09-13 показала, что «сирота» — слишком грубое
слово для первого из них: палитра `static/css/main.css` (`#080808`, `#7a3dff`,
`#ed52cb`, `#3b89ff`…) взята оттуда **дословно**. Документ не осиротел — на
него просто никто не сослался, и это разные беды с разным лечением.

Отсюда правило: **файл в корне либо назван в карте документов, либо его в
репозитории нет.** Третьего состояния — «лежит, но неизвестно зачем» — быть не
должно: именно оно позволило источнику дизайн-системы полгода выглядеть мусором,
а исследованию по Telethon — занимать место в каждом образе.

Проверяется карта, а не просто «есть ссылка откуда угодно». Ссылка из журнала
говорит, что документ когда-то упоминали; строка в карте говорит, зачем он
нужен сейчас и кому.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / "CLAUDE.md"


def _document_map() -> set[str]:
    """Имена файлов из СТРОК ТАБЛИЦЫ «Карта документов».

    Читаются только строки таблицы, а не весь раздел. Первая версия брала
    раздел целиком и сразу же покраснела на собственном пояснении: абзац под
    таблицей упоминает `Telegram_Parser_Telethon.md`, объясняя, куда он уехал,
    — и обратная проверка сочла это ссылкой на пропавший файл. Карта — это
    таблица; проза вокруг рассказывает историю, и рассказывать о том, чего
    больше нет, она обязана (за прозой следит `test_120`, который знает про
    пометки прошедшего времени).
    """
    text = CLAUDE.read_text(encoding="utf-8")
    start = text.index("## Карта документов")
    end = text.index("\n## ", start + 1)
    rows = [
        line for line in text[start:end].splitlines() if line.lstrip().startswith("|")
    ]
    return set(re.findall(r"`([\w./-]+\.md)`", "\n".join(rows)))


def _tracked_root_documents() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # только корень: документы в docs/ описаны своим каталогом
    return {name for name in out.stdout.split() if "/" not in name}


def test_the_map_is_actually_read():
    """Антивакуум: пустой разбор карты объявил бы всё названным."""
    named = _document_map()
    assert len(named) >= 5, f"в карте разобрано {len(named)} имён — разбор сломан"
    assert "AGENTS.md" in named, f"разбор карты не дал известной строки: {named}"
    assert len(_tracked_root_documents()) >= 5, "документов в корне подозрительно мало"


def test_every_document_in_the_root_is_named_in_the_map():
    unnamed = sorted(_tracked_root_documents() - _document_map())
    assert not unnamed, (
        "документ лежит в корне, но карта о нём молчит — читатель не узнает, "
        f"зачем он и можно ли ему верить: {unnamed}. Назовите его в карте или "
        "уберите из репозитория"
    )


def test_the_map_does_not_point_at_documents_that_left():
    """Обратная сторона: строка карты, пережившая свой файл, отправляет в пустоту.

    Тот же дефект, что исключение `server.py` в `ruff.toml` (12.4) и мёртвое
    правило стилей `#addChannelDrawer` (11.7): запись пережила то, что
    описывала, и продолжала утверждать обратное.
    """
    tracked = set(
        subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
    )
    dangling = sorted(name for name in _document_map() if name not in tracked)
    assert not dangling, f"карта ссылается на документы, которых нет: {dangling}"
