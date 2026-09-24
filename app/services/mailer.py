"""Почта (задача 2.9) — транспорт выбирается по среде.

production — Resend HTTP API по RESEND_API_KEY из ENV; без ключа — громкий
RuntimeError: молча проглоченное письмо сброса = молча потерянный аккаунт.
development — письмо пишется в mail_dev_dir/<timestamp>-<адрес>.html,
логируется ПУТЬ (не токен), реальных отправок из dev нет (аналог
letter_opener из Rails-шаблона).

Токен живёт только внутри письма: в логи и stderr он не попадает.
"""

import logging
import time
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_SUBJECT = "Teleton: сброс пароля"

# Точка подмены для тестов: None — обычный сетевой транспорт httpx.
RESEND_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _reset_link(token: str) -> str:
    base = get_settings().app_base_url.rstrip("/")
    # Страница подтверждения — отдельная /password-reset (задача 5.3);
    # hash-якорь SPA (#reset-password) никогда не существовал.
    return f"{base}/password-reset?token={token}"


def _render_letter(to_email: str, token: str) -> str:
    link = _reset_link(token)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>{_SUBJECT}</title></head>
<body>
<p>Здравствуйте!</p>
<p>Кто-то (надеемся, вы) запросил сброс пароля для аккаунта {to_email}.</p>
<p>Ссылка действует 1 час и срабатывает один раз:</p>
<p><a href="{link}">{link}</a></p>
<p>Если это были не вы — просто проигнорируйте письмо, пароль не изменится.</p>
</body>
</html>
"""


def _write_dev_letter(to_email: str, subject: str, html_body: str) -> Path:
    """Dev-«отправка»: письмо падает в файл-аутбокс, путь — в лог."""
    settings = get_settings()
    out_dir = Path(settings.mail_dev_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_to = to_email.replace("@", "-at-")
    path = out_dir / f"{stamp}-{safe_to}.html"
    path.write_text(html_body, encoding="utf-8")
    logger.info("dev-письмо «%s» → %s: %s", subject, to_email, path)
    return path


def _masked(address: str) -> str:
    """`sagestaf@gmail.com` → `s***@gmail.com`: найти письмо хватит, прочитать — нет."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "***"


def _resend_reason(response: httpx.Response) -> str:
    """Причина отказа словами Resend. Токена в ней нет: он живёт в теле письма."""
    try:
        return str(response.json().get("message") or "")
    except ValueError:
        return ""


async def _send_via_resend(to_email: str, subject: str, html_body: str) -> None:
    """Отправка через Resend — и строка в журнале при любом исходе (13.15).

    Раньше ни принятое письмо, ни отказ не оставляли следа: по логам нельзя
    было отличить «ушло», «отклонено» и «адреса нет в базе».
    """
    settings = get_settings()
    if not settings.resend_api_key:
        raise RuntimeError(
            "RESEND_API_KEY не задан: в production письмо сброса отправить нечем"
        )
    async with httpx.AsyncClient(timeout=10.0, transport=RESEND_TRANSPORT) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.mail_from,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
            },
        )
    if response.status_code >= 400:
        hint = ""
        if response.status_code == 403 and "@resend.dev" in settings.mail_from:
            # Самый вероятный отказ: MAIL_FROM не задан, и работает умолчание —
            # тестовый отправитель, который Resend доставляет только владельцу
            # аккаунта. Оператору нужно действие, а не код.
            hint = (
                ". Отправитель на тестовом домене resend.dev: такие письма Resend"
                " доставляет только на адрес владельца аккаунта. Нужен свой"
                " проверенный домен в Resend и адрес на нём в MAIL_FROM"
            )
        logger.error(
            "Resend отклонил письмо «%s» для %s: HTTP %s %s%s",
            subject,
            _masked(to_email),
            response.status_code,
            _resend_reason(response),
            hint,
        )
        response.raise_for_status()
    try:
        letter_id = response.json().get("id", "")
    except ValueError:
        letter_id = ""
    logger.info(
        "Resend принял письмо «%s» для %s: id=%s", subject, _masked(to_email), letter_id
    )


async def send_password_reset_email(to_email: str, token: str) -> None:
    letter = _render_letter(to_email, token)
    if get_settings().is_production:
        await _send_via_resend(to_email, _SUBJECT, letter)
    else:
        _write_dev_letter(to_email, _SUBJECT, letter)
