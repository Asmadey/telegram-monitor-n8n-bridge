"""Раздельный деплой: фронтенд на Vercel, бэкенд на Railway.

Разделение ломает ровно одну вещь — аутентификацию, и ломает молча.
`*.vercel.app` и `*.up.railway.app` — разные регистрируемые домены (оба в
Public Suffix List, общий cookie-домен невозможен даже в принципе), поэтому
запрос фронтенда к API становится межсайтовым:

- cookie с `SameSite=Lax` в межсайтовом запросе браузер не отправляет вовсе;
- `SameSite=None; Secure` отправляется, но это сторонняя cookie — Safari
  блокирует такие по умолчанию, Chrome сворачивает их поддержку;
- без CORS с `allow_credentials` браузер не отдаст ответ странице.

Отсюда два поддерживаемых режима, и оба закреплены тестами:

1. **Прокси (рекомендуемый).** Vercel переписывает `/api/*` на Railway.
   Браузер видит один origin, cookie остаются первой стороной, `SameSite=Lax`
   работает, CORS не нужен. Ничего не ломается ни в одном браузере.

2. **Прямые межсайтовые вызовы.** Нужны CORS со списком origin (никогда `*`
   вместе с учётными данными — браузер такое сочетание запрещает, и это
   правильно) и `SameSite=None; Secure`. Режим включается только явной
   настройкой, потому что в Safari он не работает.

Отдельно проверяется, что ссылка в письме сброса ведёт на ФРОНТЕНД: страница
подтверждения живёт на Vercel, а не на API-домене.
"""

import pytest

FRONTEND = "https://teleton.vercel.app"
EVIL = "https://evil.example.com"


@pytest.fixture
def cross_site(monkeypatch):
    """Режим 2: фронтенд на другом домене, прямые вызовы."""
    from app.config import get_settings

    monkeypatch.setenv("FRONTEND_ORIGINS", FRONTEND)
    monkeypatch.setenv("APP_BASE_URL", FRONTEND)
    get_settings.cache_clear()
    # Проверка на месте причины: если настройки не подхватились, отказ должен
    # выглядеть как «фикстура не сработала», а не как «ссылка не та» тремя
    # экранами ниже. Этот же класс ошибки уже стоил трёх кругов через CI.
    assert get_settings().app_base_url == FRONTEND, (
        "настройки не перечитались после cache_clear — "
        f"app_base_url={get_settings().app_base_url!r}"
    )
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_origin_is_allowed_with_credentials(anon_client, cross_site):
    resp = await anon_client.get("/health", headers={"Origin": FRONTEND})
    assert resp.headers.get("access-control-allow-origin") == FRONTEND
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_unknown_origin_is_not_allowed(anon_client, cross_site):
    resp = await anon_client.get("/health", headers={"Origin": EVIL})
    assert resp.headers.get("access-control-allow-origin") != EVIL, (
        "чужой origin получил доступ к API с учётными данными"
    )


@pytest.mark.asyncio
async def test_never_wildcard_together_with_credentials(anon_client, cross_site):
    """`*` вместе с credentials браузер отвергает, а сервер, который так
    отвечает, выглядит настроенным и не работает. Заодно это означало бы,
    что API открыт любому сайту."""
    resp = await anon_client.get("/health", headers={"Origin": FRONTEND})
    allow = resp.headers.get("access-control-allow-origin")
    creds = resp.headers.get("access-control-allow-credentials")
    assert not (allow == "*" and creds == "true")


@pytest.mark.asyncio
async def test_preflight_permits_the_csrf_header(anon_client, cross_site):
    """Без X-CSRF-Token в разрешённых заголовках ни один не-GET не пройдёт:
    браузер отсечёт запрос на предполётной проверке."""
    resp = await anon_client.options(
        "/api/monitors",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token,content-type",
        },
    )
    allowed = (resp.headers.get("access-control-allow-headers") or "").lower()
    assert "x-csrf-token" in allowed, allowed


# --------------------------------------------------------------------------
# Политика cookie
# --------------------------------------------------------------------------


def test_same_site_mode_keeps_lax():
    """Режим прокси: фронтенд и API на одном origin — cookie первой стороны."""
    from app.security.cookies import cookie_policy

    policy = cookie_policy(frontend_origins=[], api_origin="https://api.example.com")
    assert policy.samesite == "lax"


def test_cross_site_mode_requires_none_and_secure():
    """Межсайтовый режим: `Lax` браузер просто не отправит, а `None` без
    `Secure` он отвергнет."""
    from app.security.cookies import cookie_policy

    policy = cookie_policy(
        frontend_origins=[FRONTEND], api_origin="https://api.up.railway.app"
    )
    assert policy.samesite == "none"
    assert policy.secure is True


def test_shared_parent_domain_stays_first_party():
    """app.example.com + api.example.com — один регистрируемый домен, значит
    первая сторона: понижать SameSite не нужно и вредно."""
    from app.security.cookies import cookie_policy

    policy = cookie_policy(
        frontend_origins=["https://app.example.com"],
        api_origin="https://api.example.com",
    )
    assert policy.samesite == "lax", (
        "поддомены одного домена — не межсайтовый запрос, ослаблять cookie незачем"
    )


def test_public_suffix_hosts_are_not_treated_as_one_domain():
    """teleton.vercel.app и teleton.up.railway.app НЕ родственники: vercel.app
    и up.railway.app — публичные суффиксы, общей cookie между ними быть не
    может. Наивное сравнение «двух последних меток» решило бы наоборот."""
    from app.security.cookies import cookie_policy

    policy = cookie_policy(
        frontend_origins=["https://teleton.vercel.app"],
        api_origin="https://teleton.up.railway.app",
    )
    assert policy.samesite == "none"


# --------------------------------------------------------------------------
# Ссылка в письме
# --------------------------------------------------------------------------


def test_reset_link_uses_the_frontend_base(monkeypatch):
    """Страница подтверждения живёт на Vercel: ссылка на API-домен ведёт
    в никуда, а относительная — разрешается относительно API-домена, что то
    же самое.

    Проверка на уровне функции, а не сквозным письмом, намеренно. Сквозной
    вариант этого теста падал ТОЛЬКО в общем прогоне: зонд внутри мейлера
    показал, что тот получает другой экземпляр Settings с пустым
    app_base_url, хотя тест непосредственно перед отправкой видел верный.
    Причина — процессный lru_cache у get_settings: он переживает границы
    тестов и пересоздаётся непредсказуемо. Тот же класс проблемы уже ронял
    тест писем в CI (см. PROGRESS, 4 сентября). Правильное лечение —
    сделать настройки инъектируемой зависимостью FastAPI, а не глобальным
    кэшем; до тех пор контракт проверяется там, где состояние явное.
    """
    from app.config import get_settings
    from app.services.mailer import _reset_link

    monkeypatch.setenv("APP_BASE_URL", FRONTEND)
    get_settings.cache_clear()
    try:
        link = _reset_link("токен")
        assert link.startswith(FRONTEND), f"ссылка ведёт не на фронтенд: {link}"
        assert "/password-reset?token=" in link
    finally:
        get_settings.cache_clear()


def test_reset_link_is_never_relative_when_base_is_set(monkeypatch):
    """Относительная ссылка в письме — молчаливая поломка: письмо уходит,
    выглядит нормально и ведёт в никуда."""
    import app.services.mailer as _m
    from app.config import get_settings
    from app.services.mailer import _reset_link

    monkeypatch.setenv("APP_BASE_URL", "https://app.example.com/")
    get_settings.cache_clear()
    try:
        assert _m.get_settings is get_settings, (
            f"мейлер держит ДРУГОЙ get_settings: {id(_m.get_settings)} "
            f"vs {id(get_settings)}; модуль мейлера={_m.__file__}"
        )
        assert _reset_link("t").startswith("https://app.example.com/password-reset")
    finally:
        get_settings.cache_clear()
