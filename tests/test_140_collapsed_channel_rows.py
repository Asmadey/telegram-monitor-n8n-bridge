"""Свёрнутая плашка канала в окне «Параметры источника» (задача 13.7).

**Находка владельца.** У «ТОП-вакансий» девять каналов, и каждый нарисован
развёрнутым: имя, «Лимит», «Токены», корзина и текстовое поле промпта на три
строки. Чтобы дойти до девятого, надо прокрутить мимо восьми полей, которые
сейчас не нужны.

**Почему так вышло.** Модалку построила задача 11.7, когда у источника был
ровно ОДИН канал: миграция `0012` превратила каждый прежний монитор в источник
с единственным каналом. Для N=1 развёрнутый вид правильный; он деградирует
линейно и при N=9 становится непригодным. Ограничение «до 10 каналов» записано
в плане с самого начала — но ни разу не было использовано как случай для
проверки интерфейса. Покраснеть было нечему: все свипы интерфейса здесь про
ПРАВИЛЬНОСТЬ (есть ли поле, экранируется ли подстановка), а «непригодно при
девяти строках» — про СООТВЕТСТВИЕ.

**Главное ограничение, которое диктует конструкцию.** Сохранение читает DOM:
`saveSourceBtn` обходит `[data-channel-row]` и берёт `.channel-limit`,
`.channel-token-limit`, `.channel-prompt`. Если у свёрнутой строки этих полей в
документе НЕТ, `querySelector` вернёт `null`, `.value` уронит цикл, и каналы
после первого не сохранятся. Это не гипотеза: ровно это уже случалось и
закрыто `test_121`. Поэтому свёрнутость — это `hidden`, а не отсутствие, и
здесь стоит тест, который обязан это удержать.

Разметка строки проверяется НАСТОЯЩИМ исполнением в node, а не разбором
исходника: вопрос «что окажется в документе у свёрнутой строки» на регулярках
не решается, а именно он и есть цена ошибки.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"
SOURCES_JS = (JS_DIR / "sources.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")


def _node(body: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — разметку не исполнить")
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            "import { channelRowMarkup, promptBadge } from './static/js/render.js';\n"
            + body,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"не исполнилось: {result.stderr}"
    return result.stdout


CHANNEL = (
    '{channel_id: 7, chat_title: "AI Engineers Jobs", chat_target: "@ai", '
    'limit: 20, token_limit: 0, extract_prompt: "искать вакансии", '
    'check_status: "ok", check_detail: "канал «AI Engineers Jobs» — читается"}'
)
BARE = '{channel_id: 8, chat_target: "@new", limit: 20, token_limit: 0}'


def _markup(channel: str = CHANNEL) -> str:
    return _node(f"process.stdout.write(channelRowMarkup({channel}));")


# --------------------------------------------------------------------------
# Контракт сохранения переживает сворачивание
# --------------------------------------------------------------------------


def test_a_collapsed_row_still_carries_every_field_the_save_reads():
    """Урок `test_121`, который обязан пережить эту правку.

    Сохранение читает поля из документа по КАЖДОЙ строке. Если свёрнутая
    строка их не несёт, `querySelector` отдаёт `null`, `.value` роняет цикл,
    и каналы после первого молча не сохраняются.
    """
    markup = _markup()
    for field in ("channel-limit", "channel-token-limit", "channel-prompt"):
        assert f'class="{field}"' in markup or f"{field}" in markup, (
            f"у свёрнутой строки нет поля {field} — сохранение упадёт на ней "
            "и потеряет все каналы после неё"
        )


def test_the_row_is_addressable_the_way_the_save_enumerates_it():
    assert 'data-channel-row="7"' in _markup(), (
        "строка не адресуема — сохранение её не найдёт"
    )


def test_the_prompt_is_hidden_by_markup_and_not_only_by_script():
    """Урок 13.1: разметочный запрет бьёт гейт на скрипте.

    Если строка раскрыта в разметке и сворачивается скриптом, то до первого
    исполнения скрипта (или при его отказе) человек видит ровно то, от чего
    уходим, — девять развёрнутых полей.
    """
    markup = _markup()
    prompt_at = markup.index("channel-prompt")
    window = markup[max(0, prompt_at - 400) : prompt_at]
    assert "hidden" in window, (
        "развёрнутая часть строки не скрыта в самой разметке — окно "
        "открывается в том же виде, что и сейчас"
    )


# --------------------------------------------------------------------------
# Что видно в свёрнутом виде
# --------------------------------------------------------------------------


def test_the_collapsed_row_shows_the_name_and_the_limit_as_text():
    markup = _markup()
    assert "AI Engineers Jobs" in markup, "не видно имени канала"
    assert "20" in markup, "не видно лимита"


def test_the_collapsed_row_shows_the_connection_verdict():
    """Индикатор связи — вердикт проверки 13.6, а не собственная выдумка."""
    assert "check-dot-ok" in _markup(), (
        "на свёрнутой строке нет индикатора связи: проверка 13.6 посчитана, "
        "а человек её не видит"
    )


def test_a_never_checked_channel_does_not_look_connected():
    assert "check-dot-ok" not in _markup(BARE), (
        "непроверенный канал выглядит подключённым — то же самое, что "
        "«совпадений нет» вместо «не смогли посмотреть» (фаза 9)"
    )


def test_the_row_offers_editing_and_deleting():
    markup = _markup()
    assert 'data-action="edit-channel"' in markup, "нет карандаша"
    assert 'data-action="remove-channel"' in markup, "нет удаления"


# --------------------------------------------------------------------------
# Значок промпта
# --------------------------------------------------------------------------


def test_an_empty_prompt_is_marked_red_and_a_filled_one_green():
    filled = json.loads(
        _node('process.stdout.write(JSON.stringify(promptBadge("искать")));')
    )
    empty = json.loads(_node('process.stdout.write(JSON.stringify(promptBadge("")));'))
    assert filled["cls"] != empty["cls"], (
        f"заполненный и пустой промпт выглядят одинаково: {filled} / {empty}"
    )
    assert "ok" in filled["cls"], f"заполненный промпт не зелёный: {filled}"
    assert empty["title"], "у пустого промпта нет пояснения — точка без смысла"


def test_whitespace_is_not_a_prompt():
    """Пробелы и перевод строки — пустое поле, а не заполненное."""
    blank = json.loads(
        _node('process.stdout.write(JSON.stringify(promptBadge("   \\n  ")));')
    )
    empty = json.loads(_node('process.stdout.write(JSON.stringify(promptBadge("")));'))
    assert blank["cls"] == empty["cls"], f"строка из пробелов сошла за промпт: {blank}"


def test_the_badge_class_names_are_written_out_in_full():
    """Урок 13.6, пойманный на себе же: имя класса, собранное из кусков
    (`check-dot-${tone}`), не находится ничем — ни поиском по проекту, ни
    свипом осиротевших стилей."""
    render = (JS_DIR / "render.js").read_text(encoding="utf-8")
    for name in ("prompt-dot-ok", "prompt-dot-bad"):
        assert name in render, f"имя класса {name} нигде не написано целиком"
        assert name in CSS, f"класс {name} используется, но не описан в стилях"


# --------------------------------------------------------------------------
# Поведение окна
# --------------------------------------------------------------------------


def test_the_pencil_opens_only_its_own_row():
    """Раскрыть все девять по одному нажатию — это то, от чего уходим.

    Ошибка первой версии, исправлена в тесте: она искала литерал в двойных
    кавычках (`"edit-channel"`). В таком виде он есть только в РАЗМЕТКЕ
    (`data-action="edit-channel"`, `render.js`), а обработчик сравнивает
    строку в одинарных. Тест падал на `ValueError` — то есть не на утверждении
    о поведении, а на собственном разборе.
    """
    assert "edit-channel" in SOURCES_JS, "карандаш никто не слушает"
    handler = SOURCES_JS[SOURCES_JS.index("edit-channel") :][:700]
    assert "closest" in SOURCES_JS, "обработчик не ищет строку по дереву"
    assert "channel-row-edit" in handler, (
        "карандаш не раскрывает СВОЮ строку — раскроются все или ни одной"
    )


def test_deleting_a_channel_asks_first():
    """Единственное разрушающее действие кабинета без подтверждения.

    У «Удалить источник» подтверждение есть с самого начала; у канала его не
    было, и один промах необратим.
    """
    assert "confirm-remove-channel" in SOURCES_JS, (
        "удаление канала по-прежнему уходит сразу по нажатию корзины"
    )
    # Проверяется НЕДОСТИЖИМОСТЬ, а не соседство строк: удаление обязано
    # стоять за проверкой на подтверждающее действие. Первая версия смотрела
    # окно после литерала в двойных кавычках — такого в обработчике нет
    # вовсе, и тест падал на собственном разборе.
    guard = SOURCES_JS.index("confirm-remove-channel")
    deletion = SOURCES_JS.index("'DELETE'")
    assert guard < deletion, (
        "DELETE достижим раньше подтверждения — корзина удаляет сама, и "
        "«Да/Нет» ничего не решает"
    )


def test_the_window_checks_the_channels_when_it_opens():
    opener = SOURCES_JS[SOURCES_JS.index("export function openSourceModal") :][:1200]
    assert "/check" in opener or "checkChannelsOnOpen" in opener, (
        "окно не запрашивает проверку — индикатор связи покажет позавчерашнее"
    )


def test_the_arriving_verdict_does_not_wipe_what_is_being_typed():
    """Пока идёт проверка, человек правит промпт.

    Перерисовка списка стёрла бы набранное. Тот же приём, которым
    `refreshSourceTimers` обновляет счётчик, не трогая карточку (13.х).
    """
    start = SOURCES_JS.index("function applyCheckVerdicts")
    body = SOURCES_JS[start:][:900]
    assert "renderChannelRows" not in body, (
        "пришедший вердикт перерисовывает строки — всё набранное пропадёт"
    )


def test_the_prompt_badge_follows_what_is_typed_not_what_was_loaded():
    """Значок обязан показывать то, что БУДЕТ сохранено."""
    assert "channel-prompt" in SOURCES_JS, "промпт никто не слушает"
    assert "promptBadge" in SOURCES_JS, (
        "значок промпта не пересчитывается в окне: набрали промпт — значок "
        "остался красным до перезагрузки"
    )


def test_the_pending_mark_is_never_left_hanging():
    """Отказ проверки не должен выглядеть как идущая проверка.

    Значки ставятся в ⏳ до запроса. Если запрос не удался или обход не
    отчитался за отведённое время, ⏳ остался бы навсегда — вечное «идёт
    работа» там, где работы нет. Это ровно тот класс, от которого уходила вся
    фаза 9: отказ обязан отличаться от ожидания.

    Возвращаются СОХРАНЁННЫЕ вердикты, а не зелёный: показать последнее, что
    мы действительно знаем, честно; дорисовать успех — нет.
    """
    start = SOURCES_JS.index("async function checkChannelsOnOpen")
    body = SOURCES_JS[start:][:2600]
    assert body.count("applyCheckVerdicts(editing.channels)") >= 2, (
        "после отказа запроса и после истечения ожидания значки остаются в "
        "⏳ — вечное «проверяю» вместо ответа"
    )
