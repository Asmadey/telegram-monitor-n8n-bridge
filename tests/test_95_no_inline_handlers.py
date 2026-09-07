"""Инлайновых обработчиков в разметке нет — иначе интерфейс мёртв (9.8).

Найдено владельцем на живом деплое 2026-09-07: после успешного входа не
переключалась ни одна вкладка — «Сообщения», «Каналы», «Интеграции»,
«Логи» не открывались. Лента работала только потому, что помечена
активной прямо в разметке.

Причина — стык двух правильных решений. Задача 7.2 задала
`script-src 'self'` без `'unsafe-inline'`: даже прорвавшийся XSS не
исполнится. Задача 5.1 вынесла код в ES-модули, но пять кнопок вкладок
остались с `onclick="switchTab('...')"`. Инлайновый обработчик — это тот
же инлайновый скрипт: браузер молча отказывается его исполнять. В
монолите CSP не было, и всё работало.

Молча — ключевое слово. Ни ошибки на экране, ни отказа сервера: кнопка
просто не нажимается. Тесты CSP проверяли заголовок, тесты разметки —
наличие кнопок; что кнопка не сработает, не проверял никто.

Правило шире случая: любой `on*`-атрибут в разметке при нашей политике
мёртв, поэтому запрещены все.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

# Границы важны: `content="` и `data-on="` не должны считаться обработчиками.
INLINE_HANDLER = re.compile(
    r"\son(?:click|change|input|submit|load|error|"
    r"focus|blur|keyup|keydown|mouse[a-z]+)\s*=",
    re.I,
)


def test_no_page_relies_on_inline_event_handlers():
    offenders = []
    # Разметку строят и HTML-файлы, и модули: обработчик, вписанный в
    # шаблонную строку, ровно так же мёртв. Именно там их и оказалось
    # больше всего — семь против пяти в самой странице.
    pages = sorted(STATIC.glob("*.html")) + sorted((STATIC / "js").glob("*.js"))
    for page in pages:
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            if INLINE_HANDLER.search(line):
                offenders.append(f"{page.name}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "инлайновый обработчик при script-src 'self' не исполнится — "
        "элемент будет молча мёртв:\n" + "\n".join(offenders)
    )


def test_tabs_are_wired_from_the_module():
    """Раз обработчик не в разметке, он обязан быть в модуле.

    Иначе тест выше зелёный ровно тогда, когда вкладки не работают вовсе:
    убрать `onclick` и не повесить слушатель — то же самое для человека.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="messages"' in html, "у кнопок вкладок нет data-tab"

    source = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    assert "addEventListener" in source and "tab-btn" in source, (
        "вкладки ни к чему не подключены из модуля"
    )
    assert "getAttribute('onclick')" not in source, (
        "поиск активной кнопки всё ещё читает onclick — атрибута больше нет"
    )
