"""Контракт фронтенда при раздельном деплое (Vercel ↔ Railway).

Закрывает В11: фронтенд не отправлял X-CSRF-Token, и любой изменяющий запрос
к новой сборке возвращал бы 403. Заодно фиксирует две вещи, которые ломаются
молча при переезде фронтенда на отдельный домен:

- `credentials: 'same-origin'` не приложит cookie сессии к межсайтовому
  запросу — пользователь окажется разлогинен;
- запрос мимо общей обёртки уйдёт на адрес Vercel, где API нет.
"""

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
API_JS = ROOT / "static" / "js" / "api.js"
VERCEL = ROOT / "vercel.json"
PLACEHOLDER = "REPLACE-WITH-RAILWAY-HOST"


def test_api_wrapper_sends_csrf_header_on_mutations():
    """В11: без заголовка бэкенд отвергает каждый не-GET."""
    src = API_JS.read_text(encoding="utf-8")
    assert "X-CSRF-Token" in src, "обёртка не шлёт CSRF-заголовок"
    assert "csrf_token" in src, "обёртка не читает csrf-cookie"
    assert "SAFE_METHODS" in src, "заголовок должен ставиться только на не-GET"


def test_api_wrapper_includes_credentials():
    """`same-origin` молча не приложит cookie при отдельном домене."""
    src = API_JS.read_text(encoding="utf-8")
    assert "credentials: 'include'" in src
    assert "credentials: 'same-origin'" not in src


def test_api_wrapper_routes_through_a_configurable_base():
    src = API_JS.read_text(encoding="utf-8")
    assert "apiBase()" in src, "запросы не проходят через настраиваемую базу"


def test_no_module_bypasses_the_wrapper():
    """Прямой fetch() мимо обёртки не понесёт ни CSRF, ни cookie, ни базу."""
    offenders = []
    for path in sorted((ROOT / "static" / "js").glob("*.js")):
        if path.name in ("api.js", "auth-pages.js"):
            continue  # api.js — сама обёртка; страницы входа самодостаточны
        for m in re.finditer(r"(?<![.\w])fetch\s*\(", path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8").count("\n", 0, m.start()) + 1
            offenders.append(f"{path.name}:{line}")
    assert not offenders, (
        "прямой fetch мимо apiFetch — запрос уйдёт без CSRF и без cookie: "
        + ", ".join(offenders)
    )


def test_auth_pages_send_csrf_too():
    """Экраны входа не импортируют модули SPA (5.3), поэтому шлют заголовок
    сами — иначе регистрация и вход возвращают 403."""
    src = (ROOT / "static" / "js" / "auth-pages.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in src, "экраны входа не шлют CSRF-заголовок"
    assert "credentials: 'include'" in src


def test_vercel_rewrites_cover_api_and_auth():
    cfg = json.loads(VERCEL.read_text(encoding="utf-8"))
    sources = {r["source"] for r in cfg["rewrites"]}
    for required in ("/api/:path*", "/auth/:path*"):
        assert required in sources, f"нет переписывания {required}"


def test_vercel_never_proxies_over_plain_http():
    """Cookie сессии помечены Secure: по http они не доедут вовсе."""
    cfg = json.loads(VERCEL.read_text(encoding="utf-8"))
    for rule in cfg["rewrites"]:
        dest = rule["destination"]
        assert not dest.startswith("http://"), f"незащищённый переход: {dest}"


def test_vercel_spa_tabs_fall_back_to_the_shell():
    """Перезагрузка на вкладке не должна давать 404 на Vercel."""
    cfg = json.loads(VERCEL.read_text(encoding="utf-8"))
    rules = {r["source"]: r["destination"] for r in cfg["rewrites"]}
    for tab in ("/feed", "/channels", "/messages", "/integration", "/logs"):
        assert rules.get(tab) == "/index.html", f"вкладка {tab} без отката к оболочке"


def test_placeholder_is_obvious_until_replaced():
    """Плейсхолдер адреса Railway обязан остаться заметным: подставленный
    втихую неверный адрес сломал бы деплой молча."""
    raw = VERCEL.read_text(encoding="utf-8")
    if PLACEHOLDER in raw:
        assert raw.count(PLACEHOLDER) == 3, (
            "плейсхолдер заменён частично — часть запросов уйдёт не туда"
        )
