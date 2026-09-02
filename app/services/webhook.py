"""Доставка вебхуков с защитой от SSRF (задача 4.4 PLAN.md).

Оригинал (server.py:1577, POST /api/webhook/send-payload) принимал
произвольный URL — открытый прокси во внутреннюю сеть Railway:
метаданные облака (169.254.169.254), соседние сервисы, localhost.
Эндпоинт в новую сборку НЕ переносится (трипваер в test_43);
доставка (Фаза 5) идёт на СОХРАНЁННЫЙ URL пользователя — но и он
валидируется при каждой отправке: сохранённый URL мог быть заведён
до защиты.

Проверка — по РЕЗОЛВНУТОМУ IP, не по строке (п.2 плана): проверка по
имени обходится публичным доменом, который резолвится в 127.0.0.1
(localtest.me). Резолвер инъекцируется (тесты не ходят в сеть).

Диапазоны (п.3 плана + два намеренных расширения): 10/8, 172.16/12,
192.168/16, 127/8, 169.254/16, ::1, fc00::/7, fe80::/10, multicast,
reserved, unspecified; расширения — 0.0.0.0/8 (unspecified-источники)
и 100.64.0.0/10 (CGNAT: типичный адрес ДОКС внутри контейнера).
IPv4-mapped (::ffff:x.x.x.x) разворачивается до v4 ДО проверки.

Редиректы не следуются, таймаут и потолок ответа (пп.4–5): проверку
обходят не только URL, но и ответ — 302 с Location на внутренний
адрес никогда не вскрыт, гигантский ответ не выкачивается.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 64 * 1024
WEBHOOK_TIMEOUT = 10.0

# Resolver: хост → список IP (все адреса проверяются, см. тест с A-записями)
Resolver = Callable[[str], Awaitable[list[str]]]

_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",  # приватная
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local: метаданные облака
        "172.16.0.0/12",  # приватная
        "192.168.0.0/16",  # приватная
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "fc00::/7",  # unique local
        "fe80::/10",  # link local
        "ff00::/8",  # multicast
    )
)


class UnsafeWebhookURL(ValueError):
    """Вебхук-адрес ведёт в приватную/служебную сеть — запрос запрещён."""

    def __init__(self, url: str, reason: str):
        super().__init__(f"{reason} ({url})")
        self.url = url
        self.reason = reason


@dataclass
class WebhookResult:
    status_code: int
    body: bytes


def _check_ip(url: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Проверить один IP; IPv4-mapped разворачивается ДО проверки."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    networks = _BLOCKED_V4 if ip.version == 4 else _BLOCKED_V6
    for net in networks:
        if ip in net:
            raise UnsafeWebhookURL(url, f"IP {ip} в запрещённом диапазоне {net}")


async def _resolve_host(host: str) -> list[str]:
    """Живой DNS (asyncio.getaddrinfo): все A/AAAA записи хоста."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, family=socket.AF_UNSPEC)
    # info[4] — sockaddr; [0] — адрес (getaddrinfo-стабы типизируют его
    # как str | int, фактически там str)
    return list({str(info[4][0]) for info in infos})


async def validate_webhook_url(url: str, *, resolver: Resolver = _resolve_host) -> None:
    """Проверить адрес вебхука ДО любого запроса. Бросает
    UnsafeWebhookURL; литеральный IP проверяется без резолва."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURL(url, f"схема {parsed.scheme!r} — только http/https")
    host = parsed.hostname
    if not host:
        raise UnsafeWebhookURL(url, "нет хоста")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        _check_ip(url, literal)
        return

    # имя: проверяем ВСЕ резолвнутые адреса — один приватный среди
    # A-записей делает адрес запрещённым (переберу диапазоны, пока не
    # найду рабочий)
    addresses = await resolver(host)
    if not addresses:
        raise UnsafeWebhookURL(url, f"хост {host} не резолвится")
    for addr in addresses:
        _check_ip(url, ipaddress.ip_address(addr))


async def send_webhook(
    url: str,
    payload: dict,
    *,
    resolver: Resolver = _resolve_host,
    timeout: float = WEBHOOK_TIMEOUT,
    transport=None,
) -> WebhookResult:
    """POST payload на вебхук: валидация ДО запроса, без следования
    редиректам, ответ — не больше MAX_RESPONSE_BYTES."""
    await validate_webhook_url(url, resolver=resolver)
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False
    ) as client:
        async with client.stream("POST", url, json=payload) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) >= MAX_RESPONSE_BYTES:
                    break
            return WebhookResult(response.status_code, bytes(body[:MAX_RESPONSE_BYTES]))
