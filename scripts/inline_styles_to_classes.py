"""Вынос инлайновых стилей в классы-утилиты.

Инлайн обходил дизайн-систему целиком и держал в политике безопасности
`style-src 'unsafe-inline'`. Классы возвращают оба.

Одно объявление — один класс: строк вида `style="a; b; c"` 133 разных, а
объявлений в них всего 154, и половина повторяется десятки раз.
"""

import collections
import pathlib
import re
import sys

PROP = {
    "display": "d",
    "align-items": "items",
    "justify-content": "justify",
    "flex-direction": "flexdir",
    "flex-wrap": "wrap",
    "gap": "gap",
    "flex": "flex",
    "margin": "m",
    "margin-top": "mt",
    "margin-bottom": "mb",
    "margin-left": "ml",
    "margin-right": "mr",
    "padding": "p",
    "padding-top": "pt",
    "padding-bottom": "pb",
    "padding-left": "pl",
    "padding-right": "pr",
    "width": "w",
    "max-width": "maxw",
    "min-width": "minw",
    "height": "h",
    "min-height": "minh",
    "max-height": "maxh",
    "color": "c",
    "background": "bg",
    "background-color": "bg",
    "font-size": "fs",
    "font-weight": "fw",
    "line-height": "lh",
    "text-align": "ta",
    "text-transform": "tt",
    "text-decoration": "td",
    "white-space": "ws",
    "border-radius": "rad",
    "border": "bd",
    "border-left": "bdl",
    "border-bottom": "bdb",
    "border-top": "bdt",
    "overflow": "ov",
    "overflow-x": "ovx",
    "overflow-y": "ovy",
    "opacity": "op",
    "font-variant-numeric": "fvn",
    "font-family": "ff",
    "resize": "rs",
    "position": "pos",
    "top": "top",
    "left": "left",
    "right": "right",
    "bottom": "bottom",
    "z-index": "z",
    "cursor": "cur",
    "transform": "tf",
    "object-fit": "of",
    "letter-spacing": "ls",
    "box-shadow": "sh",
    "flex-shrink": "fsh",
    "word-break": "wb",
    "align-self": "self",
    "text-overflow": "tov",
    "grid-template-columns": "gtc",
    "list-style": "list",
    "vertical-align": "va",
    "user-select": "us",
}

MARK_OPEN = "@@U["
MARK_CLOSE = "]@@"


def slug(value: str) -> str:
    v = value.strip()
    v = re.sub(r"var\(--(?:space|text|rounded)-([\w-]+)\)", r"\1", v)
    v = re.sub(r"var\(--([\w-]+)\)", r"\1", v)
    v = v.replace("100%", "full").replace("%", "pct")
    v = re.sub(r"(\d)px\b", r"\1", v)
    v = v.replace(".", "_")
    v = re.sub(r"[^\w-]+", "-", v).strip("-")
    return re.sub(r"-{2,}", "-", v) or "none"


def main(paths: list[str]) -> None:
    used: collections.OrderedDict[str, str] = collections.OrderedDict()

    def to_class(decl: str) -> str:
        prop, _, value = decl.partition(":")
        prop, value = prop.strip(), value.strip()
        name = f"u-{PROP.get(prop, slug(prop))}-{slug(value)}"
        used.setdefault(name, f"{prop}: {value};")
        return name

    total = 0
    for path in paths:
        f = pathlib.Path(path)
        src = f.read_text(encoding="utf-8")

        def swap(m: re.Match) -> str:
            nonlocal total
            total += 1
            decls = [d.strip() for d in m.group(1).split(";") if d.strip()]
            return f" {MARK_OPEN}{' '.join(to_class(d) for d in decls)}{MARK_CLOSE}"

        src = re.sub(r'\s+style="([^"]*)"', swap, src)

        # Маркер сливается с существующим class= того же тега.
        def merge(tag_match: re.Match) -> str:
            tag = tag_match.group(0)
            found = re.search(
                re.escape(MARK_OPEN) + r"([^\]]*)" + re.escape(MARK_CLOSE), tag
            )
            if not found:
                return tag
            classes = found.group(1)
            tag = tag.replace(found.group(0), "").replace("  ", " ")
            if 'class="' in tag:
                return re.sub(
                    r'class="([^"]*)"',
                    lambda mm: 'class="' + (mm.group(1) + " " + classes).strip() + '"',
                    tag,
                    count=1,
                )
            closing = "/>" if tag.rstrip().endswith("/>") else ">"
            return (
                tag.rstrip()[: -len(closing)].rstrip() + f' class="{classes}"' + closing
            )

        src = re.sub(r"<[^<>]+>", merge, src)
        assert MARK_OPEN not in src, f"{f.name}: маркер остался в разметке"
        f.write_text(src, encoding="utf-8")

    css = [
        "/* Утилиты: по одному объявлению на класс.",
        " *",
        " * Заведены 2026-09-15 при выносе инлайновых стилей. Инлайн обходил",
        " * дизайн-систему целиком и держал в политике безопасности",
        " * `style-src 'unsafe-inline'` — классы возвращают и то, и другое.",
        " *",
        " * Значения здесь токены, а не числа: шкалы введены шагами 3 и 4, и",
        " * утилита с литералом снова увела бы разметку из системы.",
        " *",
        " * Имя класса УДВОЕНО в селекторе (записано дважды подряд) намеренно.",
        " * Инлайновый",
        " * стиль бил любое правило; одиночный класс — нет, и, например,",
        " * `.btn.btn-icon-sm` (специфичность 0,2,0) переигрывал утилиту",
        " * `.u-w-38` (0,1,0): кнопка обновления каталога моделей поехала с",
        " * 38px на 32px. Поймано сравнением отпечатков отрисовки.",
        " *",
        " * `!important` был бы проще, но отнял бы у скриптов возможность",
        " * менять стиль на ходу: инлайн бьёт класс, но не бьёт `!important`.",
        " * Удвоение поднимает вес ровно настолько, чтобы выиграть у составных",
        " * селекторов, и оставляет последнее слово за инлайном.",
        " */",
        "",
    ]
    for name, decl in used.items():
        css.append(f".{name}.{name} {{ {decl} }}")
    pathlib.Path("static/css/utilities.css").write_text(
        "\n".join(css) + "\n", encoding="utf-8"
    )
    print(f"вынесено инлайнов: {total} | утилит заведено: {len(used)}")


if __name__ == "__main__":
    main(sys.argv[1:])
