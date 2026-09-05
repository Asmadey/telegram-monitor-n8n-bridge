"""Задача 2.9 — почта: транспорт выбирается по среде.

Контракты:
1. development — реальных отправок НЕТ: письмо пишется в mail_dev_dir,
   логируется путь (аналог letter_opener), в файле — адрес и ссылка с
   токеном (сам токен в лог не попадает — в логе только путь).
2. production без RESEND_API_KEY — громкий RuntimeError, не молчаливый
   проглат: юзер, не получивший письмо сброса, теряет аккаунт навсегда.
3. production с ключом — уходит в Resend, файл в dev-каталоге не пишется.
"""

import pytest

from app.config import get_settings
from app.services import mailer


@pytest.fixture
def mail_env(monkeypatch, tmp_path):
    """Изолированный dev-каталог писем и фиксированный базовый URL."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-app")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MAIL_DEV_DIR", str(tmp_path / "mail"))
    monkeypatch.setenv("APP_BASE_URL", "https://teleton.example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_dev_writes_letter_file_instead_of_sending(mail_env, tmp_path):
    await mailer.send_password_reset_email("user@example.com", "TOKEN-123")

    files = list((tmp_path / "mail").glob("*.html"))
    assert files, "dev-письмо не записано в каталог-«аутбокс»"
    assert len(files) == 1, f"ожидалось ровно одно письмо, найдено {len(files)}"
    text = files[0].read_text(encoding="utf-8")
    assert "user@example.com" in text, "в письме нет адреса получателя"
    # ссылка сброса: базовый URL из ENV + токен
    assert "https://teleton.example.com" in text
    assert "TOKEN-123" in text, "в письме нет ссылки с токеном сброса"


@pytest.mark.asyncio
async def test_production_without_key_raises_loudly(mail_env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RESEND_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        await mailer.send_password_reset_email("user@example.com", "T")
    # молча проглоченное письмо = молча потерянный аккаунт


@pytest.mark.asyncio
async def test_production_with_key_sends_via_resend_not_file(
    mail_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RESEND_API_KEY", "test-key-not-a-real-secret")
    get_settings.cache_clear()

    sent: list[tuple[str, str, str]] = []

    async def fake_send(to_email: str, subject: str, html_body: str) -> None:
        sent.append((to_email, subject, html_body))

    monkeypatch.setattr(mailer, "_send_via_resend", fake_send)
    await mailer.send_password_reset_email("user@example.com", "TOKEN-XYZ")

    assert sent, "в production письмо не ушло в транспорт"
    assert sent[0][0] == "user@example.com"
    assert "TOKEN-XYZ" in sent[0][2]
    mail_dir = tmp_path / "mail"
    assert not mail_dir.exists() or not list(mail_dir.glob("*.html")), (
        "production написал dev-файл — реальная отправка не должна дублироваться в каталог"
    )
