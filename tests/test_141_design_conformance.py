"""Интерфейс сверяется с `DESIGN-webflow.md`, а не «примерно похож» на него.

CLAUDE.md называет `DESIGN-webflow.md` источником дизайн-системы: «палитра и
типографика `static/css/main.css` взяты отсюда дословно. Правите цвета —
правьте здесь же, иначе разъедется». Проверить это до сих пор было нечем, и
разъехалось ровно так, как предупреждал текст.

**Что нашла первая же сверка (2026-09-15, по просьбе владельца):**

- `--rounded-xs` **используется 12 раз и нигде не объявлен.** `border-radius`
  у всех текстовых полей молча отбрасывается: невалидное значение — это не
  ошибка, это выброшенное правило. Отсюда и литерал `2px`, однажды зашитый
  рядом, — лечили симптом, не зная причины.
- `--hairline-strong` — то же самое, три использования.
- 19 цветов совпадают с документом символ в символ. Именно поэтому остальное и
  было незаметно: палитра в порядке, а держится на честном слове.

Сверка идёт СВОИМ разборщиком, а не `yaml`: библиотека установлена
транзитивно, в `requirements-dev.txt` её нет, и тест, полагающийся на чужую
зависимость, падает в CI по причине, к делу не относящейся. Разборщик закрыт
self-test'ами — сканер, который не умеет краснеть, это зелёная строка в отчёте
и ничего больше (урок сканера XSS из 0.4).
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = (ROOT / "DESIGN-webflow.md").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
STATIC = ROOT / "static"


# --------------------------------------------------------------------------
# Разборщики
# --------------------------------------------------------------------------


def flat_section(doc: str, name: str) -> dict[str, str]:
    """Плоский раздел документа: `name:` и под ним строки `  ключ: значение`.

    Возвращает только листья. Вложенные разделы (`typography`, `components`)
    этим разборщиком не читаются — у них своя форма.
    """
    lines = doc.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == f"{name}:")
    except StopIteration:
        return {}
    out: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:  # начался следующий раздел верхнего уровня
            break
        if indent != 2:  # вложенность глубже — не наш случай
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if value:
            out[key.strip()] = value
    return out


def declared_variables(css: str) -> dict[str, str]:
    """Переменные, объявленные в таблице стилей.

    Без привязки к началу строки — и это поправка, которую потребовал
    собственный self-test. Первая версия начиналась с `^\s*`, то есть видела
    объявление, только если оно стоит первым на строке. В `main.css` так и
    есть, тест был бы зелёным — и разборщик молча пропускал бы всё, что
    записано плотнее. `var(--x)` под эту форму не подходит: за именем там нет
    двоеточия.
    """
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css)
    }


def used_variables() -> dict[str, list[str]]:
    """Все `var(--x)` в static/ — с именами файлов, чтобы отчёт был полезен."""
    used: dict[str, list[str]] = {}
    files = [
        *sorted(STATIC.glob("*.html")),
        *sorted((STATIC / "js").glob("*.js")),
        *sorted((STATIC / "css").glob("*.css")),
    ]
    for path in files:
        for name in set(
            re.findall(r"var\((--[\w-]+)", path.read_text(encoding="utf-8"))
        ):
            used.setdefault(name, []).append(path.name)
    return used


# --------------------------------------------------------------------------
# Self-test'ы разборщиков
# --------------------------------------------------------------------------


def test_the_document_parser_reads_what_it_should():
    sample = (
        "colors:\n"
        '  primary: "#080808"\n'
        "  accent-green: #00d722\n"
        "typography:\n"
        "  body-md:\n"
        "    fontSize: 16px\n"
    )
    parsed = flat_section(sample, "colors")
    assert parsed == {"primary": "#080808", "accent-green": "#00d722"}, parsed
    assert flat_section(sample, "typography") == {}, (
        "вложенный раздел разобран как плоский — значения были бы мусором"
    )
    assert flat_section(sample, "missing") == {}


def test_the_css_parser_reads_what_it_should():
    parsed = declared_variables(
        ":root { --a: 1px; --b: rgba(0,0,0,.5); }\n.x{color:red}"
    )
    assert parsed == {"--a": "1px", "--b": "rgba(0,0,0,.5)"}, parsed


# --------------------------------------------------------------------------
# Сверка
# --------------------------------------------------------------------------


def test_every_colour_of_the_document_is_in_the_stylesheet():
    colours = flat_section(DOC, "colors")
    # Страж пустоты: разборщик, вернувший ничего, обязан падать здесь, а не
    # молча объявлять, что всё совпадает.
    assert len(colours) >= 15, f"разобрано {len(colours)} цветов — это не весь раздел"

    declared = declared_variables(CSS)
    wrong = {
        name: (value, declared.get(f"--{name}"))
        for name, value in colours.items()
        if declared.get(f"--{name}", "").lower() != value.lower()
    }
    assert not wrong, (
        f"палитра разошлась с документом: {wrong}. CLAUDE.md: «правите цвета — "
        "правьте здесь же, иначе разъедется»"
    )


def test_every_radius_of_the_document_is_in_the_stylesheet():
    radii = flat_section(DOC, "rounded")
    assert len(radii) >= 4, f"разобрано {len(radii)} радиусов — это не весь раздел"

    declared = declared_variables(CSS)
    missing = {
        name: value
        for name, value in radii.items()
        if declared.get(f"--rounded-{name}") != value
    }
    assert not missing, (
        f"радиусы разошлись с документом: {missing}. Из-за такого расхождения "
        "`--rounded-xs` двенадцать раз подставлялся в пустоту, и скругление "
        "полей молча пропадало"
    )


def test_no_variable_is_used_without_being_declared():
    """Главный тест этого файла.

    Невалидное значение в CSS — не ошибка, а выброшенное правило: браузер
    молча отбрасывает объявление целиком. Поэтому опечатка или забытый токен
    выглядят не как поломка, а как «дизайнер так задумал».
    """
    declared = set(declared_variables(CSS))
    orphans = {
        name: sorted(files)
        for name, files in used_variables().items()
        if name not in declared
    }
    # `static/login.html` и соседи несут собственный `:root` — их переменные
    # объявлены у них же и в `main.css` отсутствуют законно (до шага 5, где
    # экраны входа переедут на общую палитру).
    own_root = {"--accent", "--ink", "--mute", "--hairline"}
    orphans = {
        name: files
        for name, files in orphans.items()
        if not (
            set(files) <= {"login.html", "signup.html", "password-reset.html"}
            and name in own_root
        )
    }
    assert not orphans, (
        f"переменные используются, но нигде не объявлены: {orphans}. Каждое "
        "такое место — молча выброшенное правило CSS"
    )
