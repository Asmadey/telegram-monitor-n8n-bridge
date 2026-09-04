"""Задача 5.3 — экраны входа (PLAN.md).

login.html, signup.html, password-reset.html — отдельные страницы,
НЕ часть SPA: на них не должно грузиться ничего лишнего (ни main.js
с графом вкладок, ни gsap, ни общих модулей SPA). Свои роуты GET
/login, /signup, /password-reset в новой сборке; ссылка сброса из
письма обязана вести на страницу /password-reset?token=... — раньше
она вела на hash-якорь SPA (#reset-password), которого не существует.

Уровни проверок:
- структурные: страницы существуют, содержат форму и грузят ровно один
  свой скрипт (/static/js/auth-pages.js), без внешних ресурсов; auth.js
  самодостаточен (без import-ов) и вообще не строит HTML через
  innerHTML (ошибки — через textContent, задача 5.2);
- поведенческие (ASGITransport + временная aiosqlite): страницы
  отвечают 200 text/html анониму; письмо сброса несёт ссылку на
  страницу подтверждения с токеном.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PAGES = {
    "login": ROOT / "static" / "login.html",
    "signup": ROOT / "static" / "signup.html",
    "password-reset": ROOT / "static" / "password-reset.html",
}

SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", re.I)
LINK_HREF = re.compile(r"<link\b[^>]*\bhref=\"([^\"]+)\"", re.I)


# --------------------------------------------------------------------------
# Структурные
# --------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(PAGES))
def test_auth_page_exists_with_form(slug: str) -> None:
    page = PAGES[slug]
    assert page.is_file(), f"{page} не существует"
    src = page.read_text(encoding="utf-8")
    assert "<form" in src, f"{page.name}: нет формы"


@pytest.mark.parametrize("slug", sorted(PAGES))
def test_auth_page_loads_only_its_own_script(slug: str) -> None:
    """«Ничего лишнего»: единственный скрипт страницы — общий для экранов
    входа auth-pages.js; никаких модулей SPA, gsap и внешних URL."""
    page = PAGES[slug]
    assert page.is_file(), f"{page} не существует"
    src = page.read_text(encoding="utf-8")

    script_srcs = SCRIPT_SRC.findall(src)
    assert script_srcs == ["/static/js/auth-pages.js"], (
        f"{page.name}: грузится не только свой скрипт: {script_srcs}"
    )

    forbidden = ["/static/js/main.js", "gsap", "import"]
    for marker in forbidden:
        assert marker not in src, f"{page.name}: лишняя нагрузка — {marker}"

    for href in LINK_HREF.findall(src):
        assert not re.match(r"https?://", href), (
            f"{page.name}: внешний ресурс {href} — страница обязана быть самодостаточной"
        )


def test_auth_js_is_selfcontained_and_builds_no_html() -> None:
    """auth-pages.js — крошечный общий скрипт экранов входа: без import-ов
    (не тянет граф SPA) и без innerHTML/insertAdjacentHTML — ошибки
    показываются через textContent, HTML строить нечему (5.2)."""
    path = ROOT / "static" / "js" / "auth-pages.js"
    assert path.is_file(), "static/js/auth-pages.js не существует"
    src = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*import\b", src, re.M), (
        "auth-pages.js не должен иметь import-ов"
    )
    assert ".innerHTML" not in src and ".insertAdjacentHTML" not in src, (
        "auth-pages.js строит HTML напрямую — на экранах входа нечего строить, textContent"
    )


# --------------------------------------------------------------------------
# Поведенческие
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", sorted(PAGES))
async def test_auth_page_served_to_anonymous(anon_client, slug: str) -> None:
    """Страницы входа доступны АНОНИМУ — иначе на них не попасть.
    Роуты живут в новой сборке (app/main.py), не в server.py."""
    resp = await anon_client.get(f"/{slug}")
    assert resp.status_code == 200, f"/{slug} → {resp.status_code}"
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<form" in resp.text


@pytest.mark.asyncio
async def test_reset_letter_links_to_reset_page(anon_client, db, user) -> None:
    """Ссылка сброса в письме ведёт на /password-reset?token=... —
    страница подтверждения существует и примет токен из query-парам."""
    from app.config import get_settings

    resp = await anon_client.post("/auth/password-reset", json={"email": user.email})
    assert resp.status_code == 200

    out_dir = Path(get_settings().mail_dev_dir)
    letters = list(out_dir.glob("*.html"))
    assert letters, "dev-письмо не упало в аутбокс"

    letter = letters[-1].read_text(encoding="utf-8")
    m = re.search(r"/password-reset\?token=([\w.-]+)", letter)
    assert m, f"в письме нет ссылки на страницу сброса: {letter[:200]}"
    token = m.group(1)

    # токен из письма доходит до страницы как query-парам
    page = await anon_client.get(f"/password-reset?token={token}")
    assert page.status_code == 200
