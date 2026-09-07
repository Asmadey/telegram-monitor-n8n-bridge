"""Задача 7.2 — заголовки безопасности (PLAN.md раздел 10).

CSP со script-src 'self' стала возможна после 5.1 (инлайн-скрипты вырезаны)
и 5.3 (страницы входа грузят внешний auth-pages.js без инлайн-обработчиков).
Инлайн-СТИЛИ на страницах входа остаются (5.3) — style-src несёт
'unsafe-inline' осознанно: план требует запретить именно СКРИПТЫ, стили не
исполняют код.

Красный тест плана: каждый заголовок присутствует в ответе. Проверяем не
только 200-ю страницу: заголовки обязаны быть и на 404 (страница ошибки —
тоже HTML в браузере) — middleware, а не отдельные эндпоинты.
"""

import pytest


def _csp(resp) -> str:
    return resp.headers.get("content-security-policy", "")


@pytest.mark.asyncio
async def test_security_headers_on_pages(anon_client):
    """GET / — страница: все пять заголовков из плана."""
    resp = await anon_client.get("/")
    assert resp.status_code == 200
    assert _csp(resp), "нет Content-Security-Policy"
    assert "Strict-Transport-Security" in resp.headers, "нет HSTS"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("X-Frame-Options") == "DENY"


@pytest.mark.asyncio
async def test_security_headers_on_api_and_404(anon_client):
    """/health и несуществующий путь: заголовки — middleware, не эндпоинты."""
    for path in ("/health", "/no-such-path"):
        resp = await anon_client.get(path)
        assert _csp(resp), f"нет CSP на {path}"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "max-age=" in resp.headers.get("Strict-Transport-Security", "")


@pytest.mark.asyncio
async def test_csp_forbids_inline_scripts(anon_client):
    """script-src 'self' БЕЗ 'unsafe-inline' — суть задачи: инлайн-скрипт
    (и инъекция в него) не исполнится, даже если XSS прорвётся (5.2 —
    экранирование; здесь — второй рубеж)."""
    resp = await anon_client.get("/")
    csp = _csp(resp)
    assert "script-src" in csp, "CSP без script-src"
    assert "'self'" in csp.split("script-src", 1)[1].split(";", 1)[0], (
        "script-src не ограничен 'self'"
    )
    script_directive = csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "'unsafe-inline'" not in script_directive, (
        "script-src с 'unsafe-inline' — инлайн-скрипты исполняются, "
        "запрета из задачи нет"
    )
    assert "'unsafe-eval'" not in script_directive, "unsafe-eval не нужен (сборки нет)"
    # базовые директивы замыкания: object/base/frame — классика обходов
    assert "default-src 'self'" in csp or "default-src" in csp
    assert "object-src 'none'" in csp, "object-src не запрещён (Flash/embed обходы)"
    assert "frame-ancestors 'none'" in csp, "нет frame-ancestors (кликджекинг)"
    assert "base-uri" in csp, "нет base-uri (<base> подменяет корень скриптов)"


@pytest.mark.asyncio
async def test_csp_allows_self_for_style_img_connect(anon_client):
    """Фронт живёт целиком на своём origin: стили/картинки/запросы — 'self';
    style-src с 'unsafe-inline' — осознанно (инлайн-стили страниц входа 5.3);
    img-src с data: — аватарки-заглушки/встраиваемые картинки, исполнить
    код через data:-картинку нельзя."""
    resp = await anon_client.get("/")
    csp = _csp(resp)
    style_directive = (
        csp.split("style-src", 1)[1].split(";", 1)[0] if "style-src" in csp else ""
    )
    assert "'self'" in style_directive, "style-src не 'self'"
    assert "'unsafe-inline'" in style_directive, (
        "style-src без 'unsafe-inline' сломает страницы входа (инлайн-стили 5.3)"
    )
    assert "connect-src 'self'" in csp, "нет connect-src 'self' (fetch ленты/аватарок)"
    img_directive = (
        csp.split("img-src", 1)[1].split(";", 1)[0] if "img-src" in csp else ""
    )
    assert "'self'" in img_directive, "img-src не 'self' (аватарки /api/avatars)"
    assert "data:" in img_directive, "img-src без data: — аватарки-заглушки сломаются"
