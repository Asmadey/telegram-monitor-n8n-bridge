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

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import Integration
from app.services.integrations import integration_secrets
from app.services.journal import add_log
from app.services.webhook import UnsafeWebhookURL, send_webhook, validate_webhook_url

router = APIRouter(dependencies=[Depends(require_user)])
logger = logging.getLogger(__name__)

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
        if kind == "openrouter":
            from app.services import llm

            options = payload or {}
            base_url = str(options.get("base_url") or "https://openrouter.ai/api/v1")
            model = str(options.get("model") or "")
            await validate_webhook_url(base_url)
            caller = llm.openrouter_caller(
                api_key=target,
                base_url=base_url,
                model=model,
            )
            response, tokens = await caller(
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with OK to verify this connection.",
                        }
                    ],
                }
            )
            if not response:
                raise RuntimeError("OpenRouter returned an empty response")
            return {"model": model, "response": response, "tokens": tokens}
        if kind == "telegram_bot":
            from app.services import dispatch

            chat_id = str((payload or {}).get("chat_id") or "").strip()
            if not chat_id:
                raise ValueError("Telegram recipient is empty")
            await dispatch.send_telegram_bot_message(
                target,
                chat_id,
                "✅ Teleton: тестовое сообщение доставлено.",
            )
            return {"chat_id": chat_id}
        raise NotImplementedError(kind)

    return outbound


async def _secrets(repo: TenantRepo) -> tuple[Integration | None, dict[str, str]]:
    """Строка интеграций тенанта и её расшифрованные секреты.

    Возвращается пара, а не один словарь: проверке OpenRouter нужны ещё и
    открытые поля строки (base_url, model), а проверке бота — telegram_sender_id.
    Второй запрос за той же строкой ради них — лишний круг к базе.
    """
    row = (await repo.db.scalars(repo.query(Integration))).first()
    return row, (integration_secrets(row) if row is not None else {})


async def _run_check(
    repo: TenantRepo,
    outbound,
    kind: str,
    target: str,
    missing: str,
    payload: dict | None = None,
) -> dict:
    if not target:
        raise HTTPException(status_code=400, detail=missing)
    try:
        result = await outbound(kind, target, payload)
    except UnsafeWebhookURL as exc:
        raise HTTPException(
            status_code=400, detail=f"Небезопасный адрес: {exc}"
        ) from exc
    except Exception as exc:  # сбой внешнего сервиса — не наша 500
        provider = {
            "webhook": "n8n",
            "openrouter": "OpenRouter",
            "telegram_bot": "Telegram Bot API",
        }.get(kind, "Внешний сервис")
        await add_log(
            repo.db,
            repo.user_id,
            "CHECK_FAILED",
            f"Проверка {provider} не прошла",
            "ERROR",
        )
        logger.warning("Проверка %s не прошла: %s", provider, type(exc).__name__)
        raise HTTPException(
            status_code=502, detail=f"{provider} не ответил. Повторите проверку позже."
        ) from exc
    await add_log(
        repo.db, repo.user_id, "CHECK_OK", f"Проверка {kind} прошла", "SUCCESS"
    )
    return {"status": "ok", "result": result}


@router.post("/api/webhook/test")
async def test_webhook(
    repo: TenantRepo = Depends(get_tenant_repo), outbound=Depends(get_outbound)
) -> dict:
    _, secrets = await _secrets(repo)
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
    row, secrets = await _secrets(repo)
    return await _run_check(
        repo,
        outbound,
        "openrouter",
        secrets.get("openrouter_api_key", ""),
        "Ключ OpenRouter не задан",
        {
            "base_url": row.openrouter_base_url if row else "",
            "model": row.openrouter_model if row else "",
        },
    )


@router.post("/api/telegram-forward/test")
async def test_telegram_forward(
    repo: TenantRepo = Depends(get_tenant_repo), outbound=Depends(get_outbound)
) -> dict:
    row, secrets = await _secrets(repo)
    sender_id = (row.telegram_sender_id if row else "").strip()
    if not sender_id:
        raise HTTPException(status_code=400, detail="ID получателя Telegram не задан")
    return await _run_check(
        repo,
        outbound,
        "telegram_bot",
        secrets.get("telegram_bot_token", ""),
        "Токен бота не задан",
        {"chat_id": sender_id},
    )
