"""Настройки интеграций (К2) — порт server.py:1530-1791: n8n, OpenRouter, бот.

Самая опасная часть монолита: эндпоинты открыты всему интернету, отдают сырые
ключи (К3), хранят их открытым текстом (К4) и принимают произвольный
webhook_url без проверки (К5). Шифрование и валидацию уже держит сервисный
слой — задача роутера не растерять эти гарантии.

Три правила, каждое закреплено тестом:

1. Наружу уходит только маска и признак наличия. Никогда — само значение.
2. Отсутствующее в запросе поле НЕ затирает сохранённый секрет: фронтенд
   показывает маску-плейсхолдер и шлёт форму с пустым полем, а осознанная
   очистка — это явная пустая строка (контракт С16/0.3).
3. webhook_url проверяется на SSRF ДО записи. Сохранить адрес во внутреннюю
   сеть и ходить туда по расписанию — та же дыра, просто отложенная.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import Integration
from app.services.integrations import (
    integration_secrets,
    save_integration_secrets,
    update_integration_config,
)
from app.services.journal import add_log
from app.services.webhook import UnsafeWebhookURL, validate_webhook_url

router = APIRouter(dependencies=[Depends(require_user)])

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


async def get_webhook_validator():
    """Проверка адреса вебхука — зависимостью: тест подставляет свою и не
    ходит в DNS (резолв обязателен в бою, см. задачу 4.4)."""
    return validate_webhook_url


class WebhookConfig(BaseModel):
    webhook_url: str | None = None
    auto_webhook_enabled: bool | None = None


class OpenRouterConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    is_enabled: bool | None = None


class TelegramForwardConfig(BaseModel):
    bot_token: str | None = None
    sender_id: str | None = None
    is_enabled: bool | None = None


def mask(secret: str) -> str:
    """Маска для показа: достаточно узнать ключ, недостаточно им воспользоваться."""
    if not secret:
        return ""
    if len(secret) <= 12:
        return "******"
    return f"{secret[:6]}...{secret[-4:]}"


async def _row(repo: TenantRepo) -> Integration | None:
    return (
        await repo.db.scalars(
            select(Integration).where(Integration.user_id == repo.user_id)
        )
    ).first()


async def _secrets(repo: TenantRepo) -> tuple[Integration | None, dict[str, str]]:
    row = await _row(repo)
    return row, (integration_secrets(row) if row is not None else {})


async def _apply_config(repo: TenantRepo, data: dict[str, Any]) -> None:
    """Несекретные поля — только те, что реально переданы."""
    data = {k: v for k, v in data.items() if v is not None}
    if data:
        await update_integration_config(repo.db, repo.user_id, data)


# --------------------------------------------------------------------------
# n8n webhook
# --------------------------------------------------------------------------


@router.get("/api/webhook")
async def get_webhook(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    row, secrets = await _secrets(repo)
    url = secrets.get("webhook_url", "")
    return {
        "has_webhook": bool(url),
        "webhook_url_masked": mask(url),
        "auto_webhook_enabled": bool(row.auto_webhook_enabled) if row else False,
    }


@router.post("/api/webhook")
async def save_webhook(
    req: WebhookConfig,
    repo: TenantRepo = Depends(get_tenant_repo),
    validate=Depends(get_webhook_validator),
) -> dict:
    if req.webhook_url:
        try:
            await validate(req.webhook_url)
        except UnsafeWebhookURL as exc:
            # 400 до записи: сохранённый небезопасный адрес — отложенная SSRF
            raise HTTPException(
                status_code=400, detail=f"Небезопасный адрес вебхука: {exc}"
            ) from exc

    if req.webhook_url is not None:
        await save_integration_secrets(
            repo.db, repo.user_id, webhook_url=req.webhook_url
        )
    await _apply_config(repo, {"auto_webhook_enabled": req.auto_webhook_enabled})
    await add_log(
        repo.db, repo.user_id, "SETTINGS", "Сохранены настройки n8n-вебхука", "SUCCESS"
    )
    return await get_webhook(repo)


# --------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------


@router.get("/api/openrouter")
async def get_openrouter(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    row, secrets = await _secrets(repo)
    key = secrets.get("openrouter_api_key", "")
    return {
        "has_key": bool(key),
        "api_key_masked": mask(key),
        "base_url": (row.openrouter_base_url if row else "") or DEFAULT_BASE_URL,
        "model": (row.openrouter_model if row else "") or DEFAULT_MODEL,
        "is_enabled": bool(row.openrouter_enabled) if row else False,
    }


@router.post("/api/openrouter")
async def save_openrouter(
    req: OpenRouterConfig, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    if req.api_key is not None:
        await save_integration_secrets(
            repo.db, repo.user_id, openrouter_api_key=req.api_key
        )
    await _apply_config(
        repo,
        {
            "openrouter_base_url": req.base_url,
            "openrouter_model": req.model,
            "openrouter_enabled": req.is_enabled,
        },
    )
    await add_log(
        repo.db, repo.user_id, "SETTINGS", "Сохранены настройки OpenRouter", "SUCCESS"
    )
    return await get_openrouter(repo)


# --------------------------------------------------------------------------
# Пересылка ботом
# --------------------------------------------------------------------------


@router.get("/api/telegram-forward")
async def get_telegram_forward(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    row, secrets = await _secrets(repo)
    token = secrets.get("telegram_bot_token", "")
    return {
        "has_token": bool(token),
        "bot_token_masked": mask(token),
        "sender_id": (row.telegram_sender_id if row else "") or "",
        "is_enabled": bool(row.telegram_forward_enabled) if row else False,
    }


@router.post("/api/telegram-forward")
async def save_telegram_forward(
    req: TelegramForwardConfig, repo: TenantRepo = Depends(get_tenant_repo)
) -> dict:
    if req.bot_token is not None:
        await save_integration_secrets(repo.db, repo.user_id, bot_token=req.bot_token)
    await _apply_config(
        repo,
        {
            "telegram_sender_id": req.sender_id,
            "telegram_forward_enabled": req.is_enabled,
        },
    )
    await add_log(
        repo.db,
        repo.user_id,
        "SETTINGS",
        "Сохранены настройки пересылки в Telegram",
        "SUCCESS",
    )
    return await get_telegram_forward(repo)
