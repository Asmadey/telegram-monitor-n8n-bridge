"""Карточки находок в ленте: пустые строки и отступ у названия (13.21).

Увидено на живом рендере вкладки «Лента» у источника в режиме шаблона:
между названием, фактами и описанием — по пустой строке, а название
сдвинуто вправо пробелами. Так выглядел каждый источник с
`assembly_mode=template` на проде.

Причина — встреча двух правильных по отдельности решений:

- `.feed-summary-box` держит `white-space: pre-wrap`: старые записи ленты и
  выключенный AI — это ПРОСТОЙ текст, и его переводы строк обязаны дожить
  до экрана;
- `findingCard()` собирает карточку шаблонным литералом с переносами и
  отступами между тегами — так её можно читать.

`white-space` наследуется, и внутри `pre-wrap` эти пробельные узлы между
тегами становятся видимыми: перенос — пустой строкой, отступ — пробелами
перед названием. Лечение — карточка сама объявляет `white-space: normal`;
своё объявление у элемента бьёт унаследованное значение всегда, при любом
весе и порядке правил у предка.

Браузера в CI нет, поэтому значение считает не он, а разборщик ниже: правила
из `main.css`, вес и порядок по `test_141`, НАСЛЕДОВАНИЕ — по цепочке
предков, взятой из настоящей разметки `index.html`, а не выписанной руками.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from test_141_design_conformance import rules_setting, specificity

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
FEED_JS = (ROOT / "static" / "js" / "feed.js").read_text(encoding="utf-8")

# Элемент — (тег, id, классы): ровно то, на что смотрят наши селекторы.
Element = tuple[str, str, frozenset]

_COMPOUND = re.compile(r"^([a-z][\w-]*|\*)?((?:#[\w-]+)?)((?:\.[\w-]+)*)$")


def el(tag: str, id_: str = "", *classes: str) -> Element:
    return (tag, id_, frozenset(classes))


def compound_applies(compound: str, element: Element) -> bool:
    """Достаёт ли простой селектор (`div#id.a.b`, `*`) до элемента.

    Псевдоклассы и атрибуты отбрасываются: вопрос здесь — про элемент в
    покое, а не на наведении.
    """
    match = _COMPOUND.fullmatch(compound)
    if not match or not compound:
        return False
    tag, id_part, cls = match.groups()
    if tag and tag != "*" and tag != element[0]:
        return False
    if id_part and id_part[1:] != element[1]:
        return False
    return {part for part in cls.split(".") if part} <= element[2]


def applies(selector: str, chain: list[Element]) -> bool:
    """Достаёт ли селектор до последнего элемента цепочки (предки — перед ним).

    Понимает потомка (пробел) и ребёнка (`>`). Соседи (`+`, `~`) разбором не
    распознаются и честно дают «не достаёт».
    """
    compounds: list[str] = []
    combinators: list[str] = []
    pending = " "
    for token in re.findall(r">|[^\s>]+", selector.strip()):
        if token == ">":
            pending = ">"
            continue
        if compounds:
            combinators.append(pending)
        compounds.append(token)
        pending = " "
    return bool(compounds) and _walk(compounds, combinators, chain)


def _walk(compounds: list[str], combinators: list[str], chain: list[Element]) -> bool:
    if not chain or not compound_applies(compounds[-1], chain[-1]):
        return False
    if len(compounds) == 1:
        return True
    head, joins = compounds[:-1], combinators[:-1]
    if combinators[-1] == ">":
        return _walk(head, joins, chain[:-1])
    return any(_walk(head, joins, chain[:end]) for end in range(len(chain) - 1, 0, -1))


def computed(prop: str, chain: list[Element], css: str = CSS) -> str:
    """Значение НАСЛЕДУЕМОГО свойства у последнего элемента цепочки.

    Своё объявление — по `!important`, весу и порядку; нет своего или стоит
    `inherit` — значение родителя; у корня без правил — начальное `normal`.
    """
    rows = []
    for index, (selector, value) in enumerate(rules_setting(css, prop)):
        if applies(selector, chain):
            important = value.endswith("!important")
            value = value.removesuffix("!important").strip()
            rows.append(((important, specificity(selector), index), value))
    if rows:
        value = max(rows)[1]
        if value != "inherit":
            return value
    return computed(prop, chain[:-1], css) if len(chain) > 1 else "normal"


class _Ancestry(HTMLParser):
    """Цепочка открытых элементов в момент, когда встретился нужный id."""

    VOID = {
        "area",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "wbr",
    }

    def __init__(self, target: str):
        super().__init__()
        self.target = target
        self.stack: list[Element] = []
        self.found: list[Element] | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        element = el(tag, attr.get("id") or "", *(attr.get("class") or "").split())
        if self.found is None and element[1] == self.target:
            self.found = [*self.stack, element]
        if tag not in self.VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                del self.stack[depth:]
                return


def ancestry(element_id: str, markup: str = INDEX) -> list[Element]:
    parser = _Ancestry(element_id)
    parser.feed(markup)
    assert parser.found, f"элемента #{element_id} нет в разметке"
    return parser.found


def finding_card() -> Element:
    """Корень карточки — как его пишет `findingCard()` в ленте."""
    body = FEED_JS[FEED_JS.index("function findingCard(") :]
    root = re.search(r'return html`<(\w+) class="([^"]+)"', body)
    assert root, "не нашёл корневой тег в findingCard()"
    return el(root.group(1), "", *root.group(2).split())


def summary_box() -> list[Element]:
    """Куда лента кладёт карточки: элемент, которому присваивают их разметку."""
    target = re.search(r"(\w+)\.innerHTML\s*=\s*findings\.map\(findingCard\)", FEED_JS)
    assert target, "лента больше не кладёт findings.map(findingCard) в innerHTML"
    found = re.search(
        rf"const {target.group(1)} = document\.getElementById\('([\w-]+)'\)", FEED_JS
    )
    assert found, f"не нашёл, какой элемент держит {target.group(1)}"
    return ancestry(found.group(1))


# --------------------------------------------------------------------------
# Self-test'ы разборщика: считает ли он то, что посчитал бы браузер
# --------------------------------------------------------------------------


def test_the_matcher_reads_descendants_children_ids_and_the_universal():
    box = el("div", "feedDetailSummary", "feed-summary-box")
    card = el("div", "", "finding-card")
    chain = [el("html"), el("body"), el("main", "", "panel"), box, card]
    assert applies(".finding-card", chain)
    assert applies(".feed-summary-box .finding-card", chain)
    assert applies(".panel .finding-card", chain), "потомок — на любой глубине"
    assert applies(".feed-summary-box > .finding-card", chain)
    assert not applies(".panel > .finding-card", chain), "ребёнок — только прямой"
    assert applies("#feedDetailSummary *", chain)
    assert applies("div.finding-card", chain)
    assert not applies("span.finding-card", chain)
    assert not applies(".finding-card:hover", chain), "состояние, не покой"
    assert not applies(".feed-summary-box", chain), "предок — не сам элемент"


def test_the_resolver_inherits_and_lets_the_own_rule_win():
    box = el("div", "", "box")
    card = el("div", "", "card")
    parent_only = ".box { white-space: pre-wrap; }"
    assert computed("white-space", [box, card], parent_only) == "pre-wrap", (
        "без своего правила значение приходит от родителя — ровно так и родился дефект"
    )
    own = ".card { white-space: normal; }\n.box { white-space: pre-wrap; }"
    assert computed("white-space", [box, card], own) == "normal", (
        "своё правило бьёт унаследованное даже выше по файлу и при равном весе"
    )
    heavier = ".card { white-space: normal; }\n.box .card { white-space: pre-wrap; }"
    assert computed("white-space", [box, card], heavier) == "pre-wrap"
    later = ".card { white-space: pre-wrap; }\n.card { white-space: normal; }"
    assert computed("white-space", [box, card], later) == "normal"
    forced = ".box .card { white-space: pre-wrap; }\n.card { white-space: normal !important; }"
    assert computed("white-space", [box, card], forced) == "normal"
    back = ".box { white-space: pre-wrap; }\n.card { white-space: inherit; }"
    assert computed("white-space", [box, card], back) == "pre-wrap"
    assert computed("white-space", [box, card], "") == "normal"


def test_the_ancestry_parser_survives_void_and_self_closed_tags():
    markup = (
        '<html><body><div class="a"><img src="x"><br/><input>'
        '<p class="b"><span id="t" class="c"></span></p></div></body></html>'
    )
    assert ancestry("t", markup) == [
        el("html"),
        el("body"),
        el("div", "", "a"),
        el("p", "", "b"),
        el("span", "t", "c"),
    ]


def test_the_chain_is_read_from_the_real_markup():
    chain = summary_box()
    assert chain[0][0] == "html" and chain[1][0] == "body", chain[:2]
    assert "feed-summary-box" in chain[-1][2], (
        f"карточки кладутся не в .feed-summary-box, а в {chain[-1]} — тест "
        "проверяет не тот контейнер"
    )
    assert "finding-card" in finding_card()[2]


def test_no_white_space_hides_in_an_at_rule():
    """Разборщик `test_141` читает плоские правила; внутри `@media` он слеп.

    Правило про `white-space` там прошло бы мимо — значит, его там быть не
    должно, пока разборщик этого не умеет.
    """
    sample = "@media (x) {\n  .a { color: red; }\n  .b { white-space: pre; }\n}"
    assert at_rules(sample) == [sample], "сканер не видит at-правило с двумя правилами"
    blocks = at_rules(CSS)
    assert blocks, "в main.css не нашлось ни одного @media — сканер ослеп"
    for block in blocks:
        assert "white-space" not in block, (
            f"white-space внутри at-правила: {block[:120]}"
        )


def at_rules(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"@[a-z-]+[^{;]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", css)


# --------------------------------------------------------------------------
# Сам дефект
# --------------------------------------------------------------------------


def test_the_finding_card_ignores_the_whitespace_of_its_template():
    """Пробелы между тегами шаблона не должны доходить до экрана."""
    value = computed("white-space", [*summary_box(), finding_card()])
    assert value == "normal", (
        f"карточка находки внутри .feed-summary-box получает white-space: {value}. "
        "Переносы и отступы шаблона findingCard() рисуются пустыми строками "
        "между названием, фактами и описанием и пробелами перед названием"
    )


def test_a_plain_text_summary_keeps_its_line_breaks():
    """Антивакуум: лечение не должно снять pre-wrap с самого контейнера.

    Запись ленты без находок — простой текст (`formatTelegramText`), и его
    переводы строк — это абзацы, а не мусор.
    """
    assert computed("white-space", summary_box()) == "pre-wrap"
