"""Мёртвый код интерфейса: экспорт без единого использования и чтение полей,
которых в ответе API нет (задача 12.3 `docs/PLAN.md`).

Два разных отказа с общей чертой: **код выглядит рабочим, а сработать не
может**, и потому маскирует отсутствие того, что он якобы делает.

1. `formatNextRun` (`render.js`) — экспортируется, не вызывается ниоткуда.
   Возвращает готовую HTML-строку с инлайновыми стилями, то есть вдобавок
   тянет за собой тот самый способ сборки разметки, который запрещён
   `test_48`. Живая функция такого вида — приглашение ею воспользоваться.

2. `item.photo_base64` в `feed.js` — колонки нет в модели с задачи 5.4,
   `_LIST_FIELDS` исключает её явно, а детальный вид строится тем же
   `_card`. Ветка читает поле, которого в ответе **нет ни в одном из двух
   эндпоинтов**, и никогда не срабатывает. Цена не теоретическая: она
   выглядит как запасной путь для аватарки и ровно этим прикрытием дала
   дефекту «буква вместо аватарки» прожить всю фазу 11 — на вопрос «почему
   аватарки нет» код отвечал «есть же ветка с картинкой».

Свипы 11.7 этого класса не видят: они сверяют `getElementById` с разметкой.
Здесь оба конца — данные, а стык между фронтом и API не проверял никто.

Сканеры ниже проверены обоими направлениями на синтетических источниках
(self-test'ы), как сканер XSS из задачи 0.4: сканер, который не умеет
краснеть, — это зелёная строка в отчёте и ничего больше.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"

_EXPORT = re.compile(
    r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)",
    re.M,
)


def _usages(name: str, text: str) -> int:
    """Сколько раз имя встречается как код, а не как объявление или коммент."""
    total = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if _EXPORT.match(line) and _EXPORT.match(line).group(1) == name:
            continue
        total += len(re.findall(rf"\b{re.escape(name)}\b", line))
    return total


def dead_exports(sources: dict[str, str]) -> dict[str, str]:
    """Имя → файл для каждого экспорта, который нигде не используется.

    Использованием считается любое упоминание в коде — импорт, вызов,
    ссылка из своего же модуля. Это намеренно мягче, чем «обязан быть
    импортирован»: экспорт, который используется внутри своего файла, —
    лишнее слово, а не мёртвый код.
    """
    exported: dict[str, str] = {}
    for filename, text in sources.items():
        for match in _EXPORT.finditer(text):
            exported[match.group(1)] = filename
    return {
        name: filename
        for name, filename in exported.items()
        if not any(_usages(name, text) for text in sources.values())
    }


# --------------------------------------------------------------------------
# Self-test'ы сканера экспортов: обязан краснеть и обязан не краснеть
# --------------------------------------------------------------------------


def test_scanner_flags_an_export_nobody_uses():
    found = dead_exports(
        {
            "a.js": "export function usedEverywhere() {}\nexport function nobodyCallsMe() {}\n",
            "b.js": "import { usedEverywhere } from './a.js';\nusedEverywhere();\n",
        }
    )
    assert found == {"nobodyCallsMe": "a.js"}, (
        f"сканер не увидел мёртвый экспорт: {found}"
    )


def test_scanner_does_not_flag_an_export_used_inside_its_own_file():
    """Экспорт без импорта — ещё не мёртвый код: модуль может звать себя сам."""
    found = dead_exports(
        {"a.js": "export function helper() {}\nfunction main() { helper(); }\n"}
    )
    assert found == {}, f"сканер принял живой экспорт за мёртвый: {found}"


def test_scanner_is_not_fooled_by_a_mention_in_a_comment():
    """Упоминание в комментарии — не вызов. Ровно этим и держался
    `mergeMessages`: единственная ссылка на него была строкой коммента о
    ребре, которого больше нет."""
    found = dead_exports(
        {"a.js": "// ghost и его судьба\nexport function ghost() {}\n"}
    )
    assert found == {"ghost": "a.js"}, f"комментарий засчитан за использование: {found}"


# --------------------------------------------------------------------------
# Живой свип
# --------------------------------------------------------------------------


def _spa_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js"))}


def test_sweep_actually_sees_the_spa():
    """Антивакуум: пустая выборка зеленеет сама по себе."""
    sources = _spa_sources()
    assert len(sources) >= 10, f"модулей SPA найдено {len(sources)} — выборка пуста"
    exported = {m.group(1) for text in sources.values() for m in _EXPORT.finditer(text)}
    assert len(exported) >= 25, f"экспортов найдено {len(exported)} — регулярка сломана"


def test_no_export_in_the_spa_goes_unused():
    found = dead_exports(_spa_sources())
    assert not found, (
        "экспорт, который никто не использует (мёртвый код интерфейса): "
        + ", ".join(f"{name} в {filename}" for name, filename in sorted(found.items()))
    )


# --------------------------------------------------------------------------
# Стык «фронт ↔ контракт API»
# --------------------------------------------------------------------------

# Ключи, которые `_card`/`feed_detail` добавляют к `_LIST_FIELDS` руками.
# Список держится здесь, а не выводится: он мал, и его рост должен быть
# заметен в ревью.
_EXTRA_FEED_KEYS = {"avatar_chat_id", "messages"}


def _fields_the_feed_api_sends() -> set[str]:
    src = (ROOT / "app" / "api" / "feed.py").read_text(encoding="utf-8")
    block = re.search(r"_LIST_FIELDS\s*=\s*\((.*?)\)", src, re.S)
    assert block, "в app/api/feed.py не найден _LIST_FIELDS — сканер ослеп"
    return set(re.findall(r'"([\w]+)"', block.group(1))) | _EXTRA_FEED_KEYS


def test_feed_api_field_list_is_not_empty():
    """Антивакуум: сломанный разбор `_LIST_FIELDS` пропустил бы всё подряд."""
    fields = _fields_the_feed_api_sends()
    assert len(fields) >= 10, f"полей контракта разобрано {len(fields)} — мало"
    assert "chat_title" in fields, "разбор _LIST_FIELDS не дал известного поля"


def test_feed_ui_reads_only_fields_the_api_sends():
    src = (JS_DIR / "feed.js").read_text(encoding="utf-8")
    allowed = _fields_the_feed_api_sends()
    read = {
        m.group(1)
        for line in src.splitlines()
        if not line.strip().startswith("//")
        for m in re.finditer(r"\bitem\.([A-Za-z_][\w]*)", line)
    }
    assert len(read) >= 5, f"обращений item.* найдено {len(read)} — сканер ослеп"
    unknown = read - allowed
    assert not unknown, (
        "интерфейс читает поля, которых нет в ответе API "
        f"(ветка не может сработать): {sorted(unknown)}"
    )
