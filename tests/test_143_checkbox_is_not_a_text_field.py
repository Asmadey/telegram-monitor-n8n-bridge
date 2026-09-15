"""Флажок согласия растягивался во всю строку (находка владельца).

В окне «Настройки MTProto & Авторизация Telegram» галочка «Я понимаю объём
доступа» нарисована не галочкой, а рамкой во всю ширину блока — с отступами и
границей текстового поля, а сам квадратик сидит посередине этой рамки.

**Причина — правило, написанное для полей ввода и claims'ящее любой `input`:**

    input, select, textarea { width: 100%; padding: …; border: 1px solid …; }

`input` — это и текст, и флажок, и переключатель. Остальные флажки кабинета
спрятаны под переключателем (`.switch input { width: 0 }`) и потому уцелели;
согласие 13.1 — единственный честный флажок, и он остался один на один с
геометрией текстового поля. Так было с рождения задачи 13.1 (2026-09-14), а не
привнесено уборкой дизайна: в разметке этого флажка никогда не было
инлайнового стиля, который бы это чинил.

Проверка разрешает каскад **статически**: у элемента `<input type="checkbox">`
без классов и предков смотрится, какое правило выигрывает по весу и порядку.
Живой отрисовки в CI нет, а вопрос «кто победит» — вопрос к правилам, а не к
пикселям.
"""

import re
from pathlib import Path

from test_141_design_conformance import rules_setting, specificity

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")


def applies_to_a_bare_checkbox(selector: str) -> bool:
    """Достанет ли селектор до `<input type="checkbox">` без классов и предков.

    Отбрасываются селекторы с классами, `id`, предками, соседями и
    псевдоклассами: они говорят о флажке в каком-то окружении или состоянии,
    а вопрос здесь — про одинокий флажок в покое.
    """
    value = selector.strip()
    if re.search(r"[.#>+~:\s]", value):
        return False
    if not value.startswith("input"):
        return False
    rest = value[len("input") :]
    if rest == "":
        return True
    kind = re.fullmatch(r"""\[type=["']?(\w+)["']?\]""", rest)
    return bool(kind and kind.group(1) in ("checkbox", "radio"))


def winner(prop: str) -> tuple[str, str] | None:
    """Правило, которое браузер применит к одинокому флажку.

    Больший вес побеждает; при равном весе — то, что ниже по файлу.
    """
    applicable = [
        (index, selector, value)
        for index, (selector, value) in enumerate(rules_setting(CSS, prop))
        if applies_to_a_bare_checkbox(selector)
    ]
    if not applicable:
        return None
    index, selector, value = max(
        applicable, key=lambda row: (specificity(row[1]), row[0])
    )
    return selector, value


# --------------------------------------------------------------------------
# Self-test'ы разборщика: он отвечает на вопрос «достанет ли до флажка»
# --------------------------------------------------------------------------


def test_the_matcher_knows_which_selectors_reach_a_bare_checkbox():
    assert applies_to_a_bare_checkbox("input")
    assert applies_to_a_bare_checkbox('input[type="checkbox"]')
    assert applies_to_a_bare_checkbox("input[type=radio]")


def test_the_matcher_rejects_selectors_about_other_elements_or_states():
    assert not applies_to_a_bare_checkbox(".switch input"), "предок — другое окружение"
    assert not applies_to_a_bare_checkbox("input:focus"), "состояние, а не покой"
    assert not applies_to_a_bare_checkbox('input[type="text"]'), "другой вид поля"
    assert not applies_to_a_bare_checkbox("textarea")


# --------------------------------------------------------------------------
# Сам каскад
# --------------------------------------------------------------------------


def test_a_checkbox_does_not_take_the_width_of_a_text_field():
    got = winner("width")
    assert got is not None, "ширину флажка не задаёт ни одно правило — проверять нечего"
    selector, value = got
    assert value not in ("100%", "auto%"), (
        f"флажок получает ширину текстового поля от правила «{selector}» "
        f"({value}) и растягивается во всю строку"
    )


def test_a_checkbox_does_not_take_the_frame_of_a_text_field():
    """Рамка и отступы — вторая половина того же дефекта."""
    for prop in ("border", "padding"):
        got = winner(prop)
        if got is None:
            continue
        selector, value = got
        assert "none" in value or value in ("0", "0px"), (
            f"флажок получает {prop} текстового поля от правила «{selector}» "
            f"({value}): вокруг галочки рисуется коробка поля ввода"
        )


def test_the_toggle_keeps_its_own_geometry():
    """Антивакуум: переключатель прячет свой флажок сам, и это не должно сломаться.

    У `.switch input` тот же вес, что у правила для флажков (0,1,1), и при
    равном весе побеждает последний по файлу. Значит правило для флажков
    обязано стоять ВЫШЕ, иначе все переключатели кабинета вылезут наружу.
    """
    # Комментарии срезаются: пояснение к правилу флажков САМО называет
    # `.switch input`, и поиск по сырому файлу находил его выше правила —
    # проверка краснела на собственном объяснении (ошибка теста, исправлена
    # в тесте; тот же случай, что свип осиротевших стилей в 13.6).
    code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    hidden = code.index(".switch input")
    generic = [
        match.start()
        for match in re.finditer(r"input\[type=[\"']?(?:checkbox|radio)", code)
    ]
    assert generic, "правила для флажков нет вовсе"
    assert min(generic) < hidden, (
        "правило для флажков стоит ниже `.switch input` — при равном весе "
        "побеждает оно, и скрытый флажок переключателя станет видимым"
    )
