"""Вход через Google на стороне пользователя (вторая половина Фазы 6).

Бэкенд готов и покрыт `test_60_google.py`: `POST /auth/google` проверяет
Firebase ID-токен и выдаёт СВОЮ cookie-сессию. Но в интерфейсе кнопки не
было ни одной — то есть функция существовала и была недоступна.

Кнопке нужны две вещи, и обе — предмет этого теста.

**1. Конфигурация Firebase на клиенте.** Значения (`apiKey`, `authDomain`,
`projectId`) зависят от проекта оператора, поэтому приходят из окружения
через публичный эндпоинт, а не зашиваются в HTML: иначе каждый деплой
требовал бы пересборки фронтенда. Web-`apiKey` Firebase публичен по
устройству (он идентифицирует проект, а не даёт доступ; защищают правила и
список разрешённых доменов) — но именно поэтому проверяется, что рядом с
ним не уезжает ничего из сервисного аккаунта.

**2. CSP.** Firebase SDK грузится с `www.gstatic.com` и ходит в
`identitytoolkit.googleapis.com`. При `script-src 'self'` браузер молча
откажется его запускать. Расширение CSP делается **только когда Google
настроен**: платить ослаблением политики за выключенную функцию незачем.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

GSTATIC = "https://www.gstatic.com"
IDENTITY = "https://identitytoolkit.googleapis.com"


@pytest.fixture
def google_configured(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FIREBASE_API_KEY", "AIza-test-web-key")
    monkeypatch.setenv("FIREBASE_AUTH_DOMAIN", "teleton-test.firebaseapp.com")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "teleton-test")
    get_settings.cache_clear()
    assert get_settings().firebase_project_id == "teleton-test", (
        "настройки не перечитались — отказ ниже был бы про другое"
    )
    yield
    get_settings.cache_clear()


def _csp(resp) -> str:
    return resp.headers.get("content-security-policy", "")


def _directive(csp: str, name: str) -> str:
    return csp.split(name, 1)[1].split(";", 1)[0] if name in csp else ""


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_is_public_and_says_disabled_by_default(anon_client):
    """Страница входа спрашивает конфигурацию ДО входа — эндпоинт обязан
    быть публичным. Без настройки — честное «выключено», а не 404 и не
    пустой объект, по которому клиенту нечего решать."""
    resp = await anon_client.get("/auth/google/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert not body.get("apiKey"), "ключ отдан при выключенной интеграции"


@pytest.mark.asyncio
async def test_config_exposes_only_public_web_values(anon_client, google_configured):
    resp = await anon_client.get("/auth/google/config")
    body = resp.json()
    assert body["enabled"] is True
    assert body["apiKey"] == "AIza-test-web-key"
    assert body["authDomain"] == "teleton-test.firebaseapp.com"
    assert body["projectId"] == "teleton-test"

    # рядом с публичными значениями не должно уехать НИЧЕГО из сервисного
    # аккаунта: приватный ключ Firebase даёт выпуск токенов от имени проекта
    raw = json.dumps(body).lower()
    for forbidden in ("private_key", "client_email", "begin private key", "secret"):
        assert forbidden not in raw, f"в конфигурации Firebase утечка: {forbidden}"


# --------------------------------------------------------------------------
# CSP
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csp_stays_narrow_when_google_is_not_configured(anon_client):
    """Выключенная функция не должна стоить ослабления политики."""
    csp = _csp(await anon_client.get("/login"))
    assert "gstatic" not in csp, (
        "CSP пускает сторонний скриптовый origin при выключенном входе через Google"
    )


@pytest.mark.asyncio
async def test_csp_opens_google_origins_only_when_configured(
    anon_client, google_configured
):
    csp = _csp(await anon_client.get("/login"))
    assert GSTATIC in _directive(csp, "script-src"), (
        f"Firebase SDK не загрузится: script-src = {_directive(csp, 'script-src')!r}"
    )
    assert IDENTITY in _directive(csp, "connect-src"), (
        f"вход не дойдёт до Google: connect-src = {_directive(csp, 'connect-src')!r}"
    )
    assert "frame-src" in csp, "popup входа Google не откроется без frame-src"


@pytest.mark.asyncio
async def test_inline_scripts_stay_forbidden_with_google_on(
    anon_client, google_configured
):
    """Расширение ради Firebase не должно протащить 'unsafe-inline':
    это вернуло бы XSS-исполнение, закрытое задачей 7.2."""
    csp = _csp(await anon_client.get("/login"))
    assert "'unsafe-inline'" not in _directive(csp, "script-src")


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------


@pytest.mark.parametrize("page", ["login.html", "signup.html"])
def test_pages_carry_a_google_button(page):
    html = (ROOT / "static" / page).read_text(encoding="utf-8")
    assert 'id="googleSignIn"' in html, f"{page}: нет кнопки входа через Google"
    assert "hidden" in html.split('id="googleSignIn"', 1)[1][:200], (
        f"{page}: кнопка не скрыта по умолчанию — при ненастроенном Firebase "
        "пользователь нажмёт на неработающее"
    )


def test_frontend_exchanges_the_token_for_a_session():
    """Контракт с бэкендом: страница шлёт id_token на /auth/google и
    получает свою cookie. credentials: 'include' обязателен — при отдельном
    домене фронтенда без него cookie сессии не сохранится."""
    src = (ROOT / "static" / "js" / "auth-pages.js").read_text(encoding="utf-8")
    assert "/auth/google" in src, "страница не обращается к эндпоинту входа"
    assert "id_token" in src, "токен не передаётся в поле id_token"


def test_firebase_sdk_is_pinned_to_an_exact_version():
    """Плавающая версия внешнего скрипта — это чужой код, который может
    смениться между двумя загрузками страницы входа."""
    src = (ROOT / "static" / "js" / "auth-pages.js").read_text(encoding="utf-8")
    import re

    urls = re.findall(r"https://www\.gstatic\.com/[^\s'\"]+", src)
    assert urls, "SDK Firebase не подключается"
    for url in urls:
        assert re.search(r"/\d+\.\d+\.\d+/", url), f"версия SDK не закреплена: {url}"
