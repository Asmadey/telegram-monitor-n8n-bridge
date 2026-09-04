"""Политика cookie при раздельном деплое фронтенда и API.

Фронтенд на Vercel, API на Railway — это два разных origin, и браузер
относится к запросу между ними как к межсайтовому. Последствия жёсткие:

- `SameSite=Lax` в межсайтовом запросе НЕ отправляется вовсе. Не «слабее
  защищена», а просто отсутствует: пользователь оказывается разлогинен;
- `SameSite=None` отправляется, но делает cookie сторонней. Safari блокирует
  сторонние cookie по умолчанию, Chrome сворачивает их поддержку. То есть
  межсайтовый режим работает не у всех и со временем — у всё меньшего числа;
- `None` без `Secure` браузер отвергает совсем.

Отсюда рекомендация, зафиксированная здесь в коде, а не только в документации:
**держать фронтенд и API на одном сайте.** Два способа —
переписывание `/api/*` на Vercel (браузер видит один origin) или общий
родительский домен (`app.example.com` + `api.example.com`). Оба оставляют
cookie первой стороной и `SameSite=Lax`.

Межсайтовый режим поддержан, но включается только явной настройкой
FRONTEND_ORIGINS и логируется при старте: молча ослаблять cookie нельзя.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Публичные суффиксы хостингов, которые встречаются в этом проекте. Полный
# Public Suffix List — отдельная зависимость с ежемесячными обновлениями;
# здесь важно другое: НЕ счесть родственниками `x.vercel.app` и
# `y.vercel.app`. Наивное сравнение двух последних меток сделало бы именно
# это и выдало бы общий cookie-домен `.vercel.app` — то есть cookie, видимую
# каждому чужому приложению на Vercel.
KNOWN_PUBLIC_SUFFIXES = (
    "vercel.app",
    "up.railway.app",
    "railway.app",
    "onrender.com",
    "herokuapp.com",
    "netlify.app",
    "pages.dev",
    "fly.dev",
    "github.io",
    "workers.dev",
)


@dataclass(frozen=True)
class CookiePolicy:
    samesite: str
    secure: bool
    cross_site: bool


def _host(origin: str) -> str:
    if not origin:
        return ""
    if "://" not in origin:
        origin = f"//{origin}"
    return (urlsplit(origin).hostname or "").lower().strip(".")


def registrable_domain(host: str) -> str:
    """Домен, на который вообще можно поставить общую cookie.

    Для `app.example.com` это `example.com`; для `teleton.vercel.app` —
    сам `teleton.vercel.app`, потому что `vercel.app` публичный суффикс и
    поставить cookie на него нельзя.
    """
    host = host.lower().strip(".")
    if not host or host == "localhost":
        return host
    for suffix in KNOWN_PUBLIC_SUFFIXES:
        if host == suffix:
            return host
        if host.endswith("." + suffix):
            label = host[: -len(suffix) - 1].rsplit(".", 1)[-1]
            return f"{label}.{suffix}"
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_same_site(a: str, b: str) -> bool:
    """Один ли это сайт с точки зрения браузера (схема здесь не важна)."""
    ha, hb = _host(a), _host(b)
    if not ha or not hb:
        return True  # нечего сравнивать — считаем, что разделения нет
    return registrable_domain(ha) == registrable_domain(hb)


def cookie_policy(
    *, frontend_origins: list[str], api_origin: str, force_secure: bool | None = None
) -> CookiePolicy:
    """Политика для Set-Cookie сессии и csrf-токена.

    Пустой `frontend_origins` — режим прокси или единый деплой: фронтенд
    приходит с того же origin, ослаблять cookie незачем.
    """
    cross = any(
        not is_same_site(origin, api_origin) for origin in frontend_origins if origin
    )
    if cross:
        # None без Secure браузер отвергнет — Secure здесь не опция
        logger.warning(
            "Фронтенд на другом сайте: cookie переводятся в SameSite=None. "
            "Это сторонние cookie — Safari блокирует их по умолчанию. "
            "Надёжнее переписывать /api/* на фронтенде или держать общий "
            "родительский домен."
        )
        return CookiePolicy(samesite="none", secure=True, cross_site=True)
    return CookiePolicy(
        samesite="lax",
        secure=True if force_secure is None else force_secure,
        cross_site=False,
    )
