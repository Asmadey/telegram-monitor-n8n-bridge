"""Проверочные кнопки интеграций (К2) — порт server.py:1544, 1719, 1773.

«Тестовый вебхук», «проверить ключ», «тестовое сообщение». В монолите каждая
ходит наружу прямо из обработчика и открыта всему интернету.

Два правила порта:

1. Адрес вебхука проходит ту же проверку SSRF, что и при сохранении. Иначе
   «проверить» превращается в готовый сканер внутренней сети: подставляй
   адрес, смотри на ответ. Проверка на сохранении без проверки на отправке
   закрывает только парадную дверь.

2. Ненастроенная интеграция — понятный 400, а не исключение в обработчике.
   Пользователь, нажавший «проверить» до ввода ключа, должен прочитать, чего
   не хватает, а не увидеть 500.

Исходящий вызов вынесен в зависимость: тест не ходит в сеть, а подмена
одного места покрывает все три кнопки.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.services.integrations import integration_secrets
from app.services.journal import add_log
from app.services.webhook import UnsafeWebhookURL, send_webhook, validate_webhook_url

router = APIRouter(dependencies=[Depends(require_user)])

TEST_PAYLOAD = {
    "source": "telethon_monitor",
    "event": "test_ping",
    "messages": [],
}


async def get_outbound():
    """Исходящий вызов проверки. Одна точка подмены на все три кнопки."""

    async def outbound(kind: str, target: str, payload: dict | None = None) -> Any:
        if kind == "webhook":
            await validate_webhook_url(target)
            return await send_webhook(target, payload or TEST_PAYLOAD)
        raise NotImplementedError(kind)

    return outbound


async def _secrets(repo: TenantRepo) -> dict[str, str]:
    from sqlalchemy import select

    from app.models import Integration

    row = (
        await repo.db.scalars(
            select(Integration).where(Integration.user_id == repo.user_id)
        )
    ).first()
    return integration_secrets(row) if row is not None else {}


async def _run_check(
    repo: TenantRepo, outbound, kind: str, target: str, missing: str
) -> dict:
    if not target:
        raise HTTPException(status_code=400, detail=missing)
    try:
        result = await outbound(kind, target)
    except UnsafeWebhookURL as exc:
        raise HTTPException(
            status_code=400, detail=f"Небезопасный адрес: {exc}"
        ) from exc
    except Exception as exc:  # сбой внешнего сервиса — не наша 500
        await add_log(
            repo.db, repo.user_id, "CHECK_FAILED", f"Проверка {kind}: {exc}", "ERROR"
        )
        raise HTTPException(
            status_code=502, detail=f"Проверка не прошла: {exc}"
        ) from exc
    await add_log(
        repo.db, repo.user_id, "CHECK_OK", f"Проверка {kind} прошла", "SUCCESS"
    )
    return {"status": "ok", "result": result}


@router.post("/api/webhook/test")
async def test_webhook(
    repo: TenantRepo = Depends(get_tenant_repo), outbound=Depends(get_outbound)
) -> dict:
    secrets = await _secrets(repo)
    return await _run_check(
        repo,
        outbound,
        "webhook",
        secrets.get("webhook_url", ""),
        "Адрес вебхука не задан",
    )


@router.post("/api/openrouter/test")
async def test_openrouter(
    repo: TenantRepo = Depends(get_tenant_repo), outbound=Depends(get_outbound)
) -> dict:
    secrets = await _secrets(repo)
    return await _run_check(
        repo,
        outbound,
        "openrouter",
        secrets.get("openrouter_api_key", ""),
        "Ключ OpenRouter не задан",
    )


@router.post("/api/telegram-forward/test")
async def test_telegram_forward(
    repo: TenantRepo = Depends(get_tenant_repo), outbound=Depends(get_outbound)
) -> dict:
    secrets = await _secrets(repo)
    return await _run_check(
        repo,
        outbound,
        "telegram_bot",
        secrets.get("telegram_bot_token", ""),
        "Токен бота не задан",
    )
