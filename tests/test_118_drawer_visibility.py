"""Форма, раскрываемая кнопкой, закрыта до нажатия (найдено владельцем).

Открыв «Источники», владелец сразу видел РАЗВЁРНУТУЮ форму «Новый источник»:
кнопка «+ Создать источник» ниже, форма выше и уже раскрыта. Нажать «Закрыть»
можно, открыть заново — тоже, то есть JS работал. Не работал CSS.

Причина — переименование 11.7. Раздел «Каналы» стал «Источниками», разметка и
модуль переехали с `#addChannelDrawer` на `#addSourceDrawer`, а правило в
таблице стилей осталось на прежнем имени. Правило `display: none` перестало
находить свой элемент, и форма стала видна всегда: у `.card` показ по
умолчанию, прятать её было больше нечему.

Тест держит обе стороны стыка, потому что дефект живёт ровно между ними:

- **вперёд:** id, которому JS переключает класс показа, обязан иметь правило
  в таблице стилей — иначе элемент не спрятать;
- **назад:** правило, ссылающееся на id, которого нет в разметке, — мёртвое.
  Именно мёртвое правило и пережило переименование, никого не потревожив.

Ни один существующий свип этого не ловил: разметка сама по себе верна, модуль
сам по себе верен, тесты 11.7 проверяли наличие вкладки и полей.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
MARKUP = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

# Класс, которым модули показывают свёрнутый блок.
TOGGLE_CLASS = "open"

_ELEMENT = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\(\s*['\"]([\w-]+)['\"]"
)
_TOGGLE = re.compile(
    rf"(\w+)\.classList\.(?:toggle|add|remove)\(\s*['\"]{TOGGLE_CLASS}['\"]"
)
_MARKUP_IDS = set(re.findall(r'id="([\w-]+)"', MARKUP))


def _ids_toggled_by_scripts() -> dict[str, str]:
    """id → модуль, который переключает ему класс показа."""
    found: dict[str, str] = {}
    for path in sorted((ROOT / "static" / "js").glob("*.js")):
        src = path.read_text(encoding="utf-8")
        by_var = dict(_ELEMENT.findall(src))
        for variable in _TOGGLE.findall(src):
            element_id = by_var.get(variable)
            if element_id:
                found[element_id] = path.name
    return found


def test_the_sweep_sees_something():
    """Защита от вакуумности: пустая выборка зеленела бы всегда."""
    toggled = _ids_toggled_by_scripts()
    assert toggled, (
        "не найдено ни одного элемента, которому модуль переключает класс "
        f"'{TOGGLE_CLASS}' — свип ничего не проверяет"
    )


def test_a_drawer_the_script_opens_can_also_be_closed_by_the_stylesheet():
    offenders = []
    for element_id, module in sorted(_ids_toggled_by_scripts().items()):
        if not re.search(rf"#{re.escape(element_id)}\b(?!\w)", CSS):
            offenders.append(f"#{element_id} (переключает {module})")
    assert not offenders, (
        "модуль показывает элемент классом, а спрятать его нечем — правила "
        "в таблице стилей нет, блок виден всегда:\n  " + "\n  ".join(offenders)
    )


def test_no_stylesheet_rule_points_at_an_element_that_does_not_exist():
    """Мёртвое правило — след переименования, которое доехало не везде."""
    offenders = []
    for element_id in sorted(
        set(re.findall(r"#([A-Za-z][\w-]*)\s*(?:\.\w+)?\s*\{", CSS))
    ):
        if element_id not in _MARKUP_IDS:
            offenders.append(f"#{element_id}")
    assert not offenders, (
        "правило ссылается на id, которого нет в разметке — переименование "
        "доехало не везде:\n  " + "\n  ".join(offenders)
    )
