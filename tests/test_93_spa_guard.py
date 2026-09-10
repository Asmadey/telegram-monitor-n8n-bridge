"""Интерфейс закрыт по умолчанию так же, как API (задача 9.6).

Найдено владельцем на живом деплое 2026-09-07: аноним, открывший корень,
получал полную оболочку приложения — пустые вкладки, восемь ошибок 401 в
консоли и всплывающее «Ошибка загрузки настроек». Ссылки на вход при этом
на экране не было ВООБЩЕ: страница `/login` с кнопкой Google существовала
и работала, но попасть на неё можно было только зная адрес.

Это не косметика. Задача 2.3 закрыла по умолчанию API — каждый эндпоинт
отвечает анониму 401. Интерфейс остался открытым, и результат ровно
обратный задуманному: защита сработала, а выглядит это как сломанное
приложение. Пользователь видит не «войдите», а «ничего не работает».

Проверки статические — разбор исходников фронтенда, как в 5.2 и 7.4:
поведение проверяется живым прогоном в браузере, а тест держит контракт
навсегда.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _js(name: str) -> str:
    return (STATIC / "js" / name).read_text(encoding="utf-8")


def test_spa_checks_the_session_before_it_starts_loading():
    """Оболочка не поднимается, пока не известно, кто пришёл."""
    import re

    source = _js("main.js")
    assert "/auth/me" in source, (
        "SPA стартует, не спросив, есть ли сессия: аноним получит пустое "
        "приложение вместо страницы входа"
    )

    # Первая версия сравнивала ПОЛОЖЕНИЯ строк в файле и была неверна:
    # `loadFeed()` встречается раньше проверки просто потому, что стоит
    # внутри switchTab — определения, а не запуска. Проверяется то, что
    # имелось в виду: на верхнем уровне модуля не вызывается ни один
    # загрузчик, единственный запуск — start(), внутри которого сессия
    # уже проверена.
    loaders = (
        "checkHealth",
        "loadSources",
        "loadFeed",
        "loadLogs",
        "loadCleanupConfig",
        "loadSavedMessages",
        "loadOpenRouterConfig",
        "loadTgForwardConfig",
    )
    pattern = re.compile(rf"^({'|'.join(loaders)})\s*\(")
    offenders = [line for line in source.splitlines() if pattern.match(line)]
    assert not offenders, (
        "загрузчики вызываются на верхнем уровне, до проверки сессии — "
        "те же 401 в консоли:\n" + "\n".join(offenders)
    )
    assert re.search(r"^start\(\);", source, re.M), (
        "нет единственной точки запуска, в которой сессия проверена"
    )


def test_any_401_sends_the_visitor_to_the_login_page():
    """Сессия может истечь посреди работы: это тоже не повод показывать
    стену ошибок. Один общий перехват вместо восьми отдельных тостов."""
    source = _js("api.js")
    assert "401" in source, "apiFetch не различает истёкшую сессию"
    assert "/login" in source, "истёкшая сессия никуда не ведёт"


def test_login_page_does_not_trap_an_already_signed_in_visitor():
    """Вошедший, открывший /login, должен попасть в приложение."""
    source = _js("auth-pages.js")
    assert "/auth/me" in source, (
        "страница входа не проверяет, вошёл ли уже пользователь"
    )


def test_return_path_after_login_cannot_leave_the_site():
    """`?next=` — параметр, которым управляет посетитель.

    Возврат по нему обязателен (иначе истёкшая сессия выбрасывает человека
    из середины работы на главную), но принимать можно только
    ОТНОСИТЕЛЬНЫЙ путь: `//evil.example` браузер считает адресом другого
    сайта, и страница входа превратилась бы в открытый редирект —
    удобную площадку для фишинга под доменом сервиса.
    """
    source = _js("auth-pages.js")
    assert "nextTarget" in source, "возврата после входа нет вовсе"
    guard = source.split("function nextTarget", 1)[1].split("}", 1)[0]
    assert "test(raw)" in guard or ".test(" in guard, (
        "next принимается без проверки — открытый редирект"
    )
    assert "raw : '/'" in guard, "непрошедший проверку next обязан вести на '/'"


def test_interface_offers_a_way_out_of_the_cabinet():
    """Выход из КАБИНЕТА, а не из Telegram-аккаунта.

    Кнопка «Выйти» в модалке MTProto отключает Telegram-аккаунт — это
    другое действие. Сессию кабинета закрыть было нечем.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="signOutBtn"' in html, "из кабинета невозможно выйти"
    assert 'id="accountEmail"' in html, (
        "непонятно, под кем открыт кабинет — в мульти-тенанте это важнее, "
        "чем кажется: чужая сессия в браузере выглядит как своя"
    )
    source = _js("auth.js")
    assert "/auth/logout" in source, "кнопка выхода ни к чему не подключена"


def test_hidden_attribute_actually_hides():
    """`hidden` в разметке обязан работать, а не выглядеть работающим.

    Найдено 7 сентября сразу после первой версии этой задачи: у `.btn` и
    `.status-pill` задан `display: inline-flex`, а правило браузера для
    `[hidden]` идёт без класса и проигрывает по специфичности. Кнопка
    выхода из кабинета и почта показывались анониму, хотя в разметке
    стояло `hidden`. Тест, проверяющий наличие атрибута (как test_76 для
    кнопки Google), такого не видит — нужен именно тест эффекта.
    """
    import re

    css = (STATIC / "css" / "main.css").read_text(encoding="utf-8")
    # Ищется само правило, а не первое упоминание строки «[hidden]»:
    # первая версия теста натыкалась на комментарий выше правила.
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", css)
    assert rule, "в стилях нет правила для [hidden] — атрибут бессилен"
    assert "display: none !important" in rule.group(1), (
        "правило для [hidden] не перебивает display у .btn/.status-pill"
    )


def test_shell_is_not_shown_before_the_session_is_known():
    """Аноним не должен видеть чужую панель даже мгновение до перехода."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert (
        'id="appShell"' in html and "hidden" in html.split('id="appShell"', 1)[1][:60]
    ), "оболочка отрисовывается до проверки сессии — мелькает чужая панель"
    assert "shell.hidden = false" in _js("main.js"), (
        "оболочку никто не показывает — приложение останется невидимым"
    )
