"""Перечисление строк не захватывает то, что лежит ВНУТРИ строки (владелец).

Сохранение промпта в источнике падало с `Cannot read properties of null
(reading 'value')`. Причина — один атрибут на двух вложенных элементах:

    <div class="card" data-channel-id="7">        ← строка канала
      ...
      <button data-action="remove-channel" data-channel-id="7">🗑</button>
                                           ^^^^^^^^^^^^^^^^ тот же атрибут

Сохранение обходило `querySelectorAll('[data-channel-id]')`, и выборка отдавала
не только строки, но и кнопки удаления внутри них. На кнопке
`row.querySelector('.channel-limit')` возвращает `null`, а `.value` у `null`
роняет обработчик.

Цена — не только сообщение об ошибке. Порядок обхода документный: строка,
затем кнопка внутри неё. Значит правки ПЕРВОГО канала сохранялись, обход падал
на его же кнопке, и **каналы со второго по последний не сохранялись вовсе** —
молча, при видимом «ошибка» без указания, что именно не доехало.

Почему свипы 11.7 этого не поймали: они сверяют `getElementById` с разметкой
`index.html`. Здесь элементы рождаются в шаблоне модуля, и атрибут-дубль живёт
целиком внутри JS — другой стык.

Правило, которое здесь закрепляется: **атрибут, по которому перечисляют строки,
обязан встречаться в шаблоне строки ровно один раз.** Иначе выборка once and
for all захватывает потомков, и это не видно ни в разметке, ни в тестах API.
"""

import pathlib
import re

JS_DIR = pathlib.Path(__file__).resolve().parents[1] / "static" / "js"

# querySelectorAll('[data-foo]') — перечисление ТОЛЬКО по атрибуту, без тега и
# класса: такая выборка ничем не ограничена по глубине.
_ENUMERATION = re.compile(r"""querySelectorAll\(\s*['"]\[([\w-]+)\]['"]\s*\)""")
# Шаблон разметки: html`...` или обычный литерал с тегами внутри.
_TEMPLATE = re.compile(r"`([^`]*<[a-z][^`]*)`", re.DOTALL)


def _offenders() -> list[str]:
    found = []
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        attributes = set(_ENUMERATION.findall(src))
        if not attributes:
            continue
        for template in _TEMPLATE.findall(src):
            for attribute in attributes:
                hits = len(re.findall(rf"{re.escape(attribute)}\s*=", template))
                if hits > 1:
                    line = src.count("\n", 0, src.find(template)) + 1
                    found.append(
                        f"{path.name}:~{line}: [{attribute}] встречается {hits} раза "
                        f"в одном шаблоне строки"
                    )
    return found


def test_the_sweep_sees_enumerations_at_all():
    """Защита от вакуумности: без единого перечисления тест ничего не значит."""
    total = sum(
        len(set(_ENUMERATION.findall(p.read_text(encoding="utf-8"))))
        for p in JS_DIR.glob("*.js")
    )
    assert total >= 1, "в модулях не найдено ни одного перечисления по атрибуту"


def test_row_enumeration_does_not_also_match_children():
    offenders = _offenders()
    assert not offenders, (
        "перечисление строк захватит и вложенные элементы — обработчик упадёт "
        "на первом же потомке, а строки после него не обработаются:\n  "
        + "\n  ".join(offenders)
    )
