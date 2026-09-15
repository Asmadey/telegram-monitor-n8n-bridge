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


# --------------------------------------------------------------------------
# Свип: цвет живёт в токене, а не в литерале
# --------------------------------------------------------------------------

# Экраны входа пока несут собственный `:root` и разбираются отдельным шагом.
COLOUR_FILES = [
    ROOT / "static" / "index.html",
    ROOT / "static" / "css" / "main.css",
    *sorted((ROOT / "static" / "js").glob("*.js")),
]

# Литералы, которым позволено остаться. Пусто — и это правильное состояние:
# каждая запись здесь означает цвет, живущий мимо системы.
ALLOWED_LITERALS: dict[str, str] = {}

_DECLARATION = re.compile(r"^\s*--[\w-]+\s*:")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def colour_literals() -> dict[str, list[str]]:
    """Hex-цвета вне объявлений токенов, с адресами."""
    found: dict[str, list[str]] = {}
    for path in COLOUR_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DECLARATION.match(line):
                continue  # это и есть объявление токена — ему литерал положен
            for hexcode in _HEX.findall(line):
                found.setdefault(hexcode.lower(), []).append(f"{path.name}:{number}")
    return found


def test_the_colour_scanner_sees_through_both_cases():
    """Self-test: сканер обязан отличать объявление токена от использования."""
    declaration = "      --accent-red: #ee1d36;"
    usage = "      .x { color: #ee1d36; }"
    assert _DECLARATION.match(declaration), "объявление не опознано — сканер онемеет"
    assert not _DECLARATION.match(usage), "использование принято за объявление"
    assert _HEX.findall(usage) == ["#ee1d36"]
    assert _HEX.findall("var(--accent-red)") == [], "сканер видит цвет там, где токен"


def test_no_colour_lives_outside_the_token_system():
    literals = colour_literals()
    stray = {
        hexcode: places
        for hexcode, places in literals.items()
        if hexcode not in ALLOWED_LITERALS
    }
    assert not stray, (
        f"цвета мимо системы: {len(stray)} значений, "
        f"{sum(len(p) for p in stray.values())} вхождений — {stray}. Каждый "
        "литерал живёт своей жизнью: правка палитры в документе его не догонит"
    )


# Цвет в записи `rgba(...)` — та же утечка, что hex, только незаметнее: свип
# по `#` его не видит. Нейтральная альфа при этом РАЗРЕШЕНА и это не поблажка:
# тень и затемнение под модалкой — не фирменный цвет, а примитив, и шкалы
# прозрачности в документе нет вовсе.
_NEUTRAL = {(0, 0, 0), (255, 255, 255)}
_RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def chromatic_rgba() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in COLOUR_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DECLARATION.match(line):
                continue
            for red, green, blue in _RGBA.findall(line):
                channels = (int(red), int(green), int(blue))
                if channels in _NEUTRAL:
                    continue
                found.setdefault(f"rgb{channels}", []).append(f"{path.name}:{number}")
    return found


def test_the_rgba_scanner_tells_chromatic_from_neutral():
    """Self-test: иначе свип, пропускающий всё, неотличим от свипа, которому
    нечего ловить."""
    assert _RGBA.findall("background: rgba(0, 215, 34, 0.08);") == [("0", "215", "34")]
    assert (0, 0, 0) in _NEUTRAL and (255, 255, 255) in _NEUTRAL
    assert (
        _RGBA.findall("color-mix(in srgb, var(--accent-green) 8%, transparent)") == []
    )


def test_no_chromatic_colour_hides_in_an_rgba_literal():
    stray = chromatic_rgba()
    assert not stray, (
        f"фирменный цвет записан числами: {stray}. Смена значения токена до "
        "такого места не дойдёт — это та же утечка, что hex, только её не "
        "видно свипом по «#»"
    )


# --------------------------------------------------------------------------
# Свип: размер текста живёт в шкале
# --------------------------------------------------------------------------

# Размеры, которым позволено остаться литералом. Оба — ЗНАК внутри значка
# фиксированного размера, а не текст: самый мелкий размер документа (12px) в
# кружок 13×13 не помещается. Шкала описывает текст, и растягивать её на
# глифы значило бы сломать вёрстку ради красивого отчёта.
ALLOWED_FONT_SIZES = {
    "9px": ".check-dot — знак в кружке 13×13",
    "10px": ".prompt-dot — знак в квадрате 18×18",
}

_FONT_SIZE = re.compile(r"font-size:\s*([\d.]+px)")


def font_size_literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in COLOUR_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DECLARATION.match(line):
                continue
            for size in _FONT_SIZE.findall(line):
                found.setdefault(size, []).append(f"{path.name}:{number}")
    return found


def test_the_font_size_scanner_tells_a_literal_from_a_token():
    assert _FONT_SIZE.findall("font-size: 12.5px;") == ["12.5px"]
    assert _FONT_SIZE.findall("font-size: var(--text-caption);") == [], (
        "сканер видит литерал там, где токен"
    )


def test_every_text_size_comes_from_the_scale():
    literals = font_size_literals()
    stray = {
        size: places
        for size, places in literals.items()
        if size not in ALLOWED_FONT_SIZES
    }
    assert not stray, (
        f"размеры мимо шкалы: {stray}. До 2026-09-15 их было семнадцать — "
        "включая 12.5, 11.5, 13.5 и 10.5, взятые ниоткуда: каждый экран "
        "дрейфовал сам по себе, и сравнить его было не с чем"
    )


def test_the_scale_matches_the_document():
    """Шкала — не своё изобретение: каждый токен назван ролью документа."""
    declared = declared_variables(CSS)
    expected = {
        "--text-eyebrow-sm": "12px",  # eyebrow-uppercase-sm
        "--text-caption": "12.8px",  # caption
        "--text-body-sm": "14px",  # body-sm
        "--text-body-md": "16px",  # body-md
        "--text-display-xs": "20px",  # display-xs
        "--text-display-sm": "24px",  # display-sm
        "--text-display-md": "32px",  # display-md
    }
    wrong = {
        name: (value, declared.get(name))
        for name, value in expected.items()
        if declared.get(name) != value
    }
    assert not wrong, f"шкала разошлась с ролями документа: {wrong}"


# --------------------------------------------------------------------------
# Свип: отступ живёт в шкале
# --------------------------------------------------------------------------

_SPACING_PROP = re.compile(
    r"\b(?:padding|margin|gap|row-gap|column-gap)(?:-top|-right|-bottom|-left)?\s*:"
    r"\s*([^;\"'}\n]+)"
)
_PX = re.compile(r"(-?)([\d.]+)px")


def spacing_literals() -> dict[str, list[str]]:
    """Отступы, заданные числом. Отрицательные не в счёт: сдвиг элемента —
    приём вёрстки, а не ступень шкалы."""
    found: dict[str, list[str]] = {}
    for path in COLOUR_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DECLARATION.match(line):
                continue
            for value in _SPACING_PROP.findall(line):
                for sign, digits in _PX.findall(value):
                    if sign:
                        continue
                    found.setdefault(f"{digits}px", []).append(f"{path.name}:{number}")
    return found


def test_the_spacing_scanner_reads_only_spacing():
    assert _SPACING_PROP.findall("padding: 12px 14px;") == ["12px 14px"]
    assert _SPACING_PROP.findall("gap: var(--space-sm);") == ["var(--space-sm)"]
    assert _PX.findall("var(--space-sm)") == [], "токен принят за литерал"
    # Ширина и радиус — не отступы, шкала их не касается.
    assert _SPACING_PROP.findall("width: 13px; border-radius: 2px;") == []


def test_every_gap_comes_from_the_scale():
    stray = spacing_literals()
    assert not stray, (
        f"отступы мимо шкалы: {stray}. До 2026-09-15 их было 24 разных в 272 "
        "местах, включая 4.5px; пустые состояния в шести местах имели 36, 40 "
        "и 48 — один блок, три числа"
    )


def test_the_spacing_scale_matches_the_document():
    declared = declared_variables(CSS)
    expected = flat_section(DOC, "spacing")
    assert len(expected) >= 6, f"разобрано {len(expected)} ступеней — не весь раздел"
    wrong = {
        name: (value, declared.get(f"--space-{name}"))
        for name, value in expected.items()
        if declared.get(f"--space-{name}") != value
    }
    assert not wrong, f"шкала отступов разошлась с документом: {wrong}"
