"""Живые документы не ведут в каталог, которого больше нет (перенос 2026-09-10).

Репозиторий переехал из подкаталога `Teleton/FastAPI/` в корень. Каталога
`FastAPI/` не существует ни здесь, ни в чекауте CI, поэтому указатель вида
`FastAPI/app/config.py` в живом документе ведёт в пустоту: читатель открывает
путь и не находит файла.

Почему это тест, а не разовая правка. Ровно этот класс отказа уже случался
дважды и оба раза записан в план: корневой `PLAN.md` жил вне git и показывал
склонировавшему агенту 0 из 23 закрытых задач при 17 реальных (консолидация
2026-09-04), а `PLAN_REFINEMENT.md` повторил ту же историю (раздел 14). Общее у
всех трёх случаев одно — документ пережил переезд и молча продолжил указывать
на прежнюю раскладку. Тест не даёт ей вернуться.

Что НЕ проверяется и почему. `PROGRESS.md` и строки-записи журнала в
`docs/PLAN.md` — историческая запись: на те даты раскладка действительно была
такой, и переписать её значило бы подделать журнал. Они исключены целиком.
Исключены и строки с явной пометкой времени («до 2026-09-10», «раньше»,
«прежн...») — так рассказывают о прошлом, не отправляя читателя по адресу.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

# Документы, которые читают, чтобы ДЕЙСТВОВАТЬ: по ним ходят за файлами.
LIVE_DOCS = [
    "README.md",
    "PROJECT_OVERVIEW.md",
    "AGENTS.md",
    "CLAUDE.md",
    "PLAN.md",
    "PLAN_REFINEMENT.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    *(str(p.relative_to(REPO)) for p in sorted((REPO / "docs").glob("*.md"))),
]

# Журнал: пишется один раз и не переписывается задним числом.
JOURNAL = {"PROGRESS.md"}

# Пометка времени — рассказ о прошлом, а не указатель.
HISTORY_MARKER = re.compile(
    r"до 2026-09-10|2026-09-10|раньше|прежн|тогда|истори|переехал|разворачивани",
    re.IGNORECASE,
)

# Указатель — это `FastAPI/` + сегмент пути. Запись `FastAPI/...` с многоточием
# описывает САМ ШАБЛОН пути (так о нём говорит заметка о переносе в CLAUDE.md) и
# никуда читателя не отправляет. Первая версия регулярки этого не различала и
# краснела на объяснении переноса — то есть требовала убрать текст, который как
# раз и предупреждает о старой раскладке.
POINTER = re.compile(r"(?<![\w/.])FastAPI/\w")


def _offenders() -> list[str]:
    found = []
    for name in LIVE_DOCS:
        path = REPO / name
        if not path.exists() or name in JOURNAL:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not POINTER.search(line):
                continue
            # Строка-запись журнала в docs/PLAN.md — таблица «Статус выполнения».
            if name == "docs/PLAN.md" and line.lstrip().startswith("|"):
                continue
            if HISTORY_MARKER.search(line):
                continue
            found.append(f"{name}:{number}: {line.strip()[:110]}")
    return found


def test_the_vanished_directory_is_really_gone():
    """Защита от вакуумности: если каталог вернётся, тест ниже бессмыслен."""
    assert not (REPO / "FastAPI").exists(), (
        "каталог FastAPI/ снова существует — тест на мёртвые указатели "
        "перестал что-либо значить"
    )


def test_live_documents_do_not_point_into_the_vanished_directory():
    offenders = _offenders()
    assert not offenders, (
        "живой документ отправляет читателя в каталог FastAPI/, которого нет "
        "с 2026-09-10:\n" + "\n".join(offenders)
    )
