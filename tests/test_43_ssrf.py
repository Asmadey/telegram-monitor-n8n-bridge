"""Задача 4.4 — защита от SSRF в вебхуках.

`POST /api/webhook/send-payload` (server.py:1577) принимает произвольный
payload и произвольный URL — это открытый прокси во внутреннюю сеть
Railway (метаданные облака, соседние сервисы). server.py не редактируем
(К2): в НОВОЙ сборке app/ эндпоинт вообще не появляется (трипваер ниже),
а адрес доставки валидируется ДО запроса.

Ключевой контракт (п.2 плана): проверяется РЕЗОЛВНУТЫЙ IP, а не строка
хоста — проверка по имени обходится публичным доменом, который
резолвится в 127.0.0.1 (localtest.me). Резолвер инъектируется: тесты
детерминированы и не ходят в сеть.

Блокируются: только http/https; приватные и служебные диапазоны
(10/8, 172.16/12, 192.168/16, 127/8, 169.254/16 — метаданные облака,
::1, fc00::/7, fe80::/10, v4-mapped ::ffff:x.x.x.x); редиректы НЕ
следуются (п.4 — иначе проверка обходится редиректом); таймаут и
потолок размера ответа (п.5).
"""

import httpx
import pytest

from app.services.webhook import (
    MAX_RESPONSE_BYTES,
    UnsafeWebhookURL,
    send_webhook,
    validate_webhook_url,
)


async def _resolver(mapping: dict):
    """Фейковый DNS: хост → список IP (тесты НЕ ходят в сеть)."""

    async def resolve(host: str) -> list[str]:
        return mapping[host]

    return resolve


PLAN_UNSAFE = [
    "http://127.0.0.1:8000/",
    "http://169.254.169.254/latest/meta-data/",  # метаданные облака
    "http://10.0.0.1/",
    "http://192.168.0.1/",
    "http://172.16.0.1/",
    "http://[::1]/",
    "http://[fc00::1]/",  # unique local
    "http://[fe80::1]/",  # link local
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped обход
    "file:///etc/passwd",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", PLAN_UNSAFE)
async def test_ssrf_urls_rejected(url):
    """Литеральные опасные адреса и не-http схемы — UnsafeWebhookURL."""
    with pytest.raises(UnsafeWebhookURL):
        await validate_webhook_url(url)


@pytest.mark.asyncio
async def test_public_name_resolving_to_loopback_rejected():
    """ГЛАВНЫЙ контракт: публичное ИМЯ → 127.0.0.1 (localtest.me) —
    строка хоста безопасна, IP нет. Проверка обязана резолвить."""
    resolver = await _resolver({"localtest.me": ["127.0.0.1"]})
    with pytest.raises(UnsafeWebhookURL) as exc:
        await validate_webhook_url("http://localtest.me/", resolver=resolver)
    # в причине — сам IP (диагностика в логах)
    assert "127.0.0.1" in str(exc.value), "причина не называет IP"


@pytest.mark.asyncio
async def test_rejected_when_any_resolved_ip_is_private():
    """Один приватный среди A-записей — адрес запрещён: переберут все,
    пока не найдут рабочий."""
    resolver = await _resolver({"dual.example.com": ["203.0.113.5", "10.0.0.9"]})
    with pytest.raises(UnsafeWebhookURL):
        await validate_webhook_url("http://dual.example.com/hook", resolver=resolver)


@pytest.mark.asyncio
async def test_public_urls_pass():
    """Легитимные вебхуки не ломаем: публичный литерал и публичное имя."""
    resolver = await _resolver({"example.com": ["93.184.216.34"]})
    # литерал не должен требовать DNS
    await validate_webhook_url("http://93.184.216.34/hook")
    await validate_webhook_url("https://example.com/webhook", resolver=resolver)


@pytest.mark.asyncio
async def test_ip_literal_never_hits_dns():
    """Литеральный IP проверяется без резолва (резолвер поднят бы
    AssertionError — его вызов значит, что проверка пошла в сеть)."""

    async def booby_trap(host: str) -> list[str]:
        raise AssertionError(f"резолв ненужен для литерала, а вызван: {host}")

    await validate_webhook_url("http://93.184.216.34/hook", resolver=booby_trap)


# --- доставка: валидация до запроса, без редиректов, потолок ответа ---


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b'{"status":"ok"}')


@pytest.mark.asyncio
async def test_send_webhook_validates_before_any_request():
    """Опасный URL — ни одного байта в сеть; валидация НЕ опциональна."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(UnsafeWebhookURL):
        await send_webhook(
            "http://127.0.0.1:9000/probe",
            {"x": 1},
            transport=httpx.MockTransport(handler),
        )
    assert calls == [], "запрос ушёл в сеть ДО/МИМО валидации"


@pytest.mark.asyncio
async def test_send_webhook_does_not_follow_redirects():
    """302 на внутренний адрес НЕ следуется (п.4 плана): один запрос,
    ответ редиректа отдан как есть, Location не вскрыт."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )

    result = await send_webhook(
        "http://93.184.216.34/hook", {"x": 1}, transport=httpx.MockTransport(handler)
    )
    assert len(calls) == 1, "клиент ПОСЛЕДОВАЛ за редиректом на внутренний адрес"
    assert result.status_code == 302


@pytest.mark.asyncio
async def test_send_webhook_caps_response_size():
    """Ответ больше потолка не читается целиком (п.5): чужой вебхук не
    выкачивает память процесса мегабайтами."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (4 * MAX_RESPONSE_BYTES))

    result = await send_webhook(
        "http://93.184.216.34/hook", {"x": 1}, transport=httpx.MockTransport(handler)
    )
    assert len(result.body) <= MAX_RESPONSE_BYTES, "ответ прочитан целиком, без потолка"


@pytest.mark.asyncio
async def test_send_webhook_hardens_client(monkeypatch):
    """Клиент доставки создаётся с follow_redirects=False и таймаутом —
    параметры не «забыть» при рефакторинге."""
    from app.services import webhook as webhook_module

    captured: dict = {}
    original = httpx.AsyncClient

    class Spy(original):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(webhook_module.httpx, "AsyncClient", Spy)
    await send_webhook(
        "http://93.184.216.34/hook",
        {"x": 1},
        transport=httpx.MockTransport(_ok_handler),
    )
    assert captured.get("follow_redirects") is False, "редиректы не выключены"
    assert captured.get("timeout") == webhook_module.WEBHOOK_TIMEOUT, "таймаута нет"


def test_send_payload_endpoint_absent(app):
    """Трипваер (зелёный с рождения): /api/webhook/send-payload —
    открытый прокси — в новой сборке НЕ существует; при переносе
    вебхуков из server.py (Фаза 5) эндпоинт не портируется."""
    paths = set()

    def walk(routes):
        for route in routes:
            if hasattr(route, "path"):
                paths.add(route.path)
            if hasattr(route, "routes"):
                walk(route.routes)

    walk(app.routes)
    assert "/api/webhook/send-payload" not in paths, "прокси-эндпоинт вернулся в сборку"
