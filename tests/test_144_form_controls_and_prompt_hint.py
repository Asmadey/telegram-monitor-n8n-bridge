"""Форма источника: контролы разной высоты и непонятный промпт (находки владельца).

Три вещи, увиденные в одном окне:

1. **Список интервала выше поля названия.** Отступы и шрифт у них одни и те
   же — правило `input, select, textarea` одно на всех, — а высота разная:
   34,5px против 36,5px. Нативный `select` считает свою внутреннюю геометрию
   сам, и наши отступы её не выравнивают. Заодно он рисует свою стрелку,
   которая живёт вне дизайн-системы.

2. **Кнопки «Из диалогов» и «+ Добавить» ниже поля рядом с ними** — 25px
   против 34,5px: они `btn-sm`, а стоят в одной строке с полем полного
   размера.

3. **«Промпт оформления ответа» не объясняет, когда он используется.**
   Человек видит пустое поле и не знает, что будет, если его не заполнить, —
   а разница настоящая: без него в ленту уходит результат разбора как есть
   (именно так JSON вместо HTML и попадал в бота).

Высоту здесь считает не тест, а браузер — в CI его нет. Поэтому проверяется
то, из чего высота складывается: вертикальные отступы, размер текста и рамка
у соседних контролов обязаны совпадать, а `select` обязан отказаться от
нативной геометрии (`appearance: none`), иначе наши отступы ни при чём.
"""

import re
from pathlib import Path

from test_141_design_conformance import rules_setting, specificity

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

_SIMPLE = re.compile(
    r"""^([a-z][\w-]*)?((?:\[type=["']?\w+["']?\])?)((?:\.[\w-]+)*)$"""
)


def applies(
    selector: str, tag: str, *, kind: str = "", classes: frozenset = frozenset()
):
    """Достанет ли селектор до элемента: тег, `type=` и набор классов.

    Селекторы с предками, соседями и псевдоклассами отбрасываются: они
    говорят об элементе в окружении или состоянии, а вопрос здесь — про
    контрол в покое.
    """
    match = _SIMPLE.fullmatch(selector.strip())
    if not match:
        return False
    name, typed, cls = match.group(1), match.group(2), match.group(3)
    if name and name != tag:
        return False
    if typed:
        wanted = re.fullmatch(r"""\[type=["']?(\w+)["']?\]""", typed).group(1)
        if wanted != kind:
            return False
    needed = {part for part in cls.split(".") if part}
    return needed <= set(classes)


def winner(prop: str, tag: str, *, kind: str = "", classes: frozenset = frozenset()):
    """Значение, которое победит по весу и порядку, или None."""
    rows = [
        (index, selector, value)
        for index, (selector, value) in enumerate(rules_setting(CSS, prop))
        if applies(selector, tag, kind=kind, classes=classes)
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: (specificity(row[1]), row[0]))[2]


def vertical(padding: str) -> str:
    return padding.split()[0] if padding else ""


FIELD = dict(tag="input", kind="text")
ADD_CHANNEL_BUTTONS = ("openDialogsModalBtn", "addChannelBtn")


# --------------------------------------------------------------------------
# Self-test'ы разборщика
# --------------------------------------------------------------------------


def test_the_matcher_reads_tags_types_and_classes():
    assert applies("select", "select")
    assert applies("input", "input", kind="text")
    assert applies('input[type="checkbox"]', "input", kind="checkbox")
    assert not applies('input[type="checkbox"]', "input", kind="text")
    assert applies(".btn", "button", classes=frozenset({"btn", "btn-field"}))
    assert not applies(".btn.btn-icon-sm", "button", classes=frozenset({"btn"})), (
        "составной селектор требует ВСЕ свои классы"
    )
    assert not applies(".card .btn", "button", classes=frozenset({"btn"}))
    assert not applies("select:focus", "select")


# --------------------------------------------------------------------------
# Список и поле — одной высоты
# --------------------------------------------------------------------------


def test_the_dropdown_gives_up_the_native_geometry():
    """Пока `select` нативный, высоту считает браузер, а не наши отступы."""
    assert winner("appearance", "select") == "none", (
        "у списка не снята нативная отрисовка: при тех же отступах он выходит "
        "выше поля ввода, и в строке контролы стоят разной высоты"
    )


def test_the_dropdown_and_the_field_share_what_makes_height():
    for prop, read in (("padding", vertical), ("font-size", str), ("border", str)):
        field = winner(prop, **FIELD)
        dropdown = winner(prop, "select")
        assert read(field) == read(dropdown), (
            f"{prop} у списка и поля разные ({dropdown!r} против {field!r}) — "
            "высота разойдётся"
        )


def test_the_dropdown_arrow_is_drawn_from_the_palette():
    """Своя стрелка — значит своя, а не зашитая цветом мимо системы."""
    rule = CSS[CSS.index("\n    select {") :]
    rule = rule[: rule.index("}")]
    assert "background-image" in rule, "стрелка не нарисована вовсе"
    assert "var(--" in rule, f"цвет стрелки взят не из токена: {rule}"


# --------------------------------------------------------------------------
# Кнопки в строке с полем
# --------------------------------------------------------------------------


def _classes_of(element_id: str) -> frozenset:
    tag = re.search(rf'<button[^>]*id="{element_id}"[^>]*>', INDEX)
    assert tag, f"кнопки {element_id} нет в разметке"
    classes = re.search(r'class="([^"]*)"', tag.group(0))
    assert classes, f"у кнопки {element_id} нет классов: {tag.group(0)}"
    return frozenset(classes.group(1).split())


def test_the_buttons_next_to_the_field_are_as_tall_as_the_field():
    field_padding = vertical(winner("padding", **FIELD))
    field_size = winner("font-size", **FIELD)
    for element_id in ADD_CHANNEL_BUTTONS:
        classes = _classes_of(element_id)
        padding = vertical(winner("padding", "button", classes=classes))
        size = winner("font-size", "button", classes=classes)
        assert (padding, size) == (field_padding, field_size), (
            f"кнопка {element_id} ниже поля рядом с ней: отступы {padding!r} "
            f"против {field_padding!r}, текст {size!r} против {field_size!r}"
        )


def test_the_field_sized_button_stays_below_the_small_one():
    """Антивакуум: вес одинаковый (0,1,0), при равном весе решает порядок."""
    code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert code.index(".btn-field") > code.index(".btn-sm"), (
        "правило кнопки ростом с поле стоит выше `.btn-sm` — тот его переиграет"
    )


# --------------------------------------------------------------------------
# Промпт оформления объясняет себя
# --------------------------------------------------------------------------


def _hint_after(textarea_id: str) -> str:
    start = INDEX.index(f'id="{textarea_id}"')
    tail = INDEX[start : start + 1200]
    hint = re.search(r"<p[^>]*>(.*?)</p>", tail, re.S)
    return hint.group(1).strip() if hint else ""


def test_the_formatting_prompt_says_when_it_runs_and_what_empty_means():
    """Пустое поле без пояснения — это вопрос «а что будет, если не заполню».

    Ответ настоящий и неочевидный: без промпта оформления в ленту и в бота
    уходит результат разбора как есть — именно так JSON вместо сообщения и
    попадал в бота 15 сентября.
    """
    for textarea_id in ("sourceAnswerPrompt", "editSourceAnswerPrompt"):
        hint = _hint_after(textarea_id).lower()
        assert hint, f"у поля {textarea_id} нет пояснения вовсе"
        assert "шаг" in hint or "после" in hint, (
            f"пояснение не говорит, КОГДА промпт работает: {hint!r}"
        )
        assert "пуст" in hint or "не заполн" in hint, (
            f"пояснение не говорит, что будет, если поле оставить пустым: {hint!r}"
        )
