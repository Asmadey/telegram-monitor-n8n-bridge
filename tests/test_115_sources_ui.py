"""Вкладка «Источники»: каналы, лимиты и промпты в одном месте (11.7).

До сих пор интерфейс знал только «канал = монитор»: одна ссылка, один
промпт, один лимит. Источник из пяти каналов собрать было нечем, хотя
модель, конвейер и API это уже умеют.

Проверки статические — разбор разметки и модулей. Они не заменяют живой
клик, но ловят то, что ломалось в этом проекте раз за разом: поле,
которого нет в ответе (9.10), обработчик, который не исполняется под CSP
(9.8), подстановка без экранирования (0.4) и модалка, которую умеют
закрывать, но не открывать (9.18).
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS_DIR = ROOT / "static" / "js"
SOURCES_JS = JS_DIR / "sources.js"


def test_tab_is_named_sources():
    """Раздел называется «Источники»: канал — вход, а не объект."""
    assert 'data-tab="sources"' in INDEX, "вкладка источников не заведена"
    assert re.search(r'data-tab="sources"[^>]*>\s*Источники', INDEX), (
        "вкладка есть, но названа не «Источники»"
    )
    assert 'data-tab="channels"' not in INDEX, "старая вкладка «Каналы» осталась"


def test_source_module_exists_and_is_loaded():
    assert SOURCES_JS.exists(), "нет модуля static/js/sources.js"
    assert "js/sources.js" in INDEX or "sources.js" in (
        (JS_DIR / "main.js").read_text(encoding="utf-8")
    ), "модуль источников никуда не подключён"


def test_source_form_asks_for_what_the_pipeline_needs():
    """Имя, интервал, стоп-слова и промпт оформления — поля ИСТОЧНИКА."""
    for element in (
        "sourceTitle",
        "sourceInterval",
        "sourceStopWords",
        "sourceAnswerPrompt",
    ):
        assert f'id="{element}"' in INDEX, f"в форме источника нет поля {element}"


def test_channel_row_carries_its_own_limit_and_prompt():
    """Решение владельца: у каждого канала свой лимит и свой промпт."""
    source = SOURCES_JS.read_text(encoding="utf-8")
    assert "data-channel-id" in source, "строка канала не адресуема"
    for field in ("channel-limit", "channel-prompt"):
        assert field in source, f"в строке канала нет поля {field}"


def test_source_is_addressed_by_public_id():
    """Контракт 9.10: `id` в ответе нет, адресуем по public_id."""
    source = SOURCES_JS.read_text(encoding="utf-8")
    offenders = [
        line.strip()[:90]
        for line in source.splitlines()
        if re.search(r"\bs\.id\b|\bsource\.id\b|\bitem\.id\b", line)
    ]
    assert not offenders, (
        "источник адресуется полем, которого API не отдаёт:\n" + "\n".join(offenders)
    )
    assert 'data-source-id="${s.public_id}"' in source, (
        "кнопки карточки источника не получают идентификатор"
    )


def test_run_button_is_disabled_while_running():
    """Два тапа — двойной счёт за токены; сервер идемпотентен, но и кнопка
    не должна выглядеть работающей, пока прогон идёт."""
    source = SOURCES_JS.read_text(encoding="utf-8")
    assert "disabled" in source, "кнопка запуска нигде не блокируется"


def test_save_requires_at_least_one_channel():
    """Источник без каналов не опрашивается — сохранять его бессмысленно."""
    source = SOURCES_JS.read_text(encoding="utf-8")
    assert re.search(r"channels\.length|каналов", source), (
        "нет проверки, что у источника есть хотя бы один канал"
    )


def test_no_inline_handlers_in_the_new_markup():
    """Контракт 9.8: при script-src 'self' инлайновый обработчик мёртв."""
    offenders = re.findall(r'on(?:click|change|input|submit)\s*=\s*"', INDEX)
    assert not offenders, f"инлайновые обработчики в разметке: {offenders[:5]}"


def test_every_interpolation_in_the_module_is_escaped():
    """Контракт 0.4: имя канала пишет не сервис, а владелец канала."""
    source = SOURCES_JS.read_text(encoding="utf-8")
    raw_blocks = re.findall(r"innerHTML\s*=\s*`", source)
    assert not raw_blocks, (
        "прямой innerHTML со строкой: подстановка должна идти через html``, "
        "который экранирует"
    )


def test_every_element_the_modules_reach_for_exists():
    """Модуль не должен искать элемент, которого нет в разметке.

    Переезд на источники унёс старую форму добавления канала, а вместе с
    ней кнопку «Из диалогов» — обработчик остался, элемента не стало.
    Защита `if (button)` спасает от исключения, но не от того, что функция
    молча исчезает из интерфейса: тот же класс, что модалка, которую умеют
    закрывать и не умеют открывать (9.18).
    """
    # Идентификаторы собираются со ВСЕХ страниц: `auth-pages.js` работает
    # на login.html и signup.html, и сверять его с одним index.html было бы
    # неверно — первая версия свипа именно так и ошиблась.
    ids: set[str] = set()
    for page in sorted((ROOT / "static").glob("*.html")):
        ids |= set(re.findall(r'id="([^"]+)"', page.read_text(encoding="utf-8")))
    missing: dict[str, list[str]] = {}
    for module in sorted(JS_DIR.glob("*.js")):
        if module.name == "main.js" or "vendor" in module.parts:
            continue
        used = set(
            re.findall(
                r"getElementById\('([^']+)'\)", module.read_text(encoding="utf-8")
            )
        )
        gone = sorted(used - ids)
        if gone:
            missing[module.name] = gone
    assert not missing, f"модули ищут несуществующие элементы: {missing}"
