"""Задача 3.4 — шифрование секретов тенантов + отказ старта без ключа.

Контракты плана:
1. шифротекст не содержит открытого текста, дешифровка возвращает исходное;
2. дешифровка чужим ключом — InvalidToken;
3. секреты интеграций (bot token, openrouter key, webhook url) в БД
   ТОЛЬКО зашифрованными — колонки *_encrypted;
4. приложение ОТКАЗЫВАЕТСЯ СТАРТОВАТЬ при отсутствующем или коротком
   APP_ENCRYPTION_KEY — без явного отказа кто-нибудь однажды запустит
   прод с ключом по умолчанию (или «key»), и MTProto-сессии чужих
   аккаунтов, удалённо не сбрасываемые, окажутся под ним.

Проверка старта — в ПОДПРОЦЕССЕ: import app.main в живом процессе
тестов уже случился (с валидным ключом из conftest), повторно его
не воспроизвести. ENV процесса подменяется целиком: реальные ENV
могут содержать боевой APP_ENCRYPTION_KEY, .env — тоже (pydantic
берёт ENV поверх .env, поэтому ключ ЗАТИРАЕМ, а не удаляем).
"""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.security.crypto import decrypt, encrypt
from app.services.integrations import (
    integration_secrets,
    save_integration_secrets,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

BOT_TOKEN = "77123456789:AAE-fake-bot-token"
OPENROUTER_KEY = "sk-or-fake-v1-0123456789abcdef"
WEBHOOK_URL = "https://n8n.example.com/webhook/fake-secret-path"
SECRET_COLUMNS = {
    "telegram_bot_token_encrypted": BOT_TOKEN,
    "openrouter_api_key_encrypted": OPENROUTER_KEY,
    "webhook_url_encrypted": WEBHOOK_URL,
}


def _settings_with(key: str):
    """Псевдо-конфиг с заданным ключом — для подмены get_settings в crypto."""
    return types.SimpleNamespace(app_encryption_key=key)


def _run_import(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """Импорт app.main в чистом процессе с заданным ENV.

    ENV затирает .env (pydantic-settings: окружение важнее файла), поэтому
    ключ и URL базы задаются явно — проверяем именно поведение при
    ПЛОХОМ/ОТСУТСТВУЮЩЕМ ключе, а не при попутно отсутствующей базе.
    """
    env = dict(os.environ)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_ciphertext_hides_plaintext_and_roundtrips(_env):
    """Шифротекст не содержит открытого текста; дешифровка = исходное."""
    secret = "one-two-three-four-five-six"
    token = encrypt(secret)
    assert secret not in token, "открытый текст виден в шифротексте"
    assert decrypt(token) == secret, "дешифровка не вернула исходное"


def test_wrong_key_raises_invalid_token(monkeypatch):
    """Дешифровка чужим ключом — InvalidToken: подделка не проходит молча."""
    import app.security.crypto as crypto

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with(key_a))
    token = encrypt("secret-under-key-a")
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with(key_b))
    with pytest.raises(InvalidToken):
        decrypt(token)


@pytest.mark.asyncio
async def test_integration_secrets_encrypted_at_rest(db, user_a):
    """Секреты интеграций — только зашифрованными: открытого текста в
    колонках нет, расшифровка возвращает исходное, upsert не плодит строк."""
    row = await save_integration_secrets(
        db,
        user_a.id,
        bot_token=BOT_TOKEN,
        openrouter_api_key=OPENROUTER_KEY,
        webhook_url=WEBHOOK_URL,
    )
    assert row.id, "строка integrations не сохранена"

    raw = (
        await db.execute(
            text(
                "SELECT telegram_bot_token_encrypted, openrouter_api_key_encrypted, "
                "webhook_url_encrypted FROM integrations WHERE user_id = :u"
            ),
            {"u": user_a.id},
        )
    ).first()
    assert raw, "integrations не заполнена"
    for column, ciphertext in zip(SECRET_COLUMNS, raw):
        assert SECRET_COLUMNS[column] not in ciphertext, (
            f"{column}: секрет лежит открытым текстом"
        )
    secrets = integration_secrets(row)
    assert secrets["telegram_bot_token"] == BOT_TOKEN
    assert secrets["openrouter_api_key"] == OPENROUTER_KEY
    assert secrets["webhook_url"] == WEBHOOK_URL


@pytest.mark.asyncio
async def test_partial_update_keeps_other_secrets(db, user_a):
    """Повторное сохранение ОДНОГО секрета не затирает остальные и не
    плодит строки (unique user_id — upsert)."""
    await save_integration_secrets(db, user_a.id, bot_token=BOT_TOKEN)
    await save_integration_secrets(db, user_a.id, openrouter_api_key=OPENROUTER_KEY)
    count = (
        await db.execute(
            text("SELECT COUNT(*) FROM integrations WHERE user_id = :u"),
            {"u": user_a.id},
        )
    ).scalar()
    assert count == 1, "upsert породил вторую строку integrations"

    from sqlalchemy import select as sa_select

    from app.models import Integration

    row = (
        await db.scalars(sa_select(Integration).where(Integration.user_id == user_a.id))
    ).first()
    secrets = integration_secrets(row)
    assert secrets["telegram_bot_token"] == BOT_TOKEN, (
        "частичное обновление затёрло прежний bot token"
    )
    assert secrets["openrouter_api_key"] == OPENROUTER_KEY


def test_app_refuses_to_start_without_key():
    """Без APP_ENCRYPTION_KEY приложение не стартует — с внятной причиной."""
    proc = _run_import(
        {"APP_ENCRYPTION_KEY": "", "DATABASE_URL": "sqlite+aiosqlite://"}
    )
    assert proc.returncode != 0, (
        f"приложение завелось без APP_ENCRYPTION_KEY: {proc.stdout}"
    )
    assert "APP_ENCRYPTION_KEY" in proc.stderr, (
        f"отказ без ключа не назвал причину: {proc.stderr[-500:]}"
    )


def test_app_refuses_to_start_with_short_key():
    """Короткий ключ («key», «short») — тоже отказ: молча слабое шифрование
    не лучше отсутствующего."""
    proc = _run_import(
        {"APP_ENCRYPTION_KEY": "short", "DATABASE_URL": "sqlite+aiosqlite://"}
    )
    assert proc.returncode != 0, (
        f"приложение завелось с коротким APP_ENCRYPTION_KEY: {proc.stdout}"
    )
    assert "APP_ENCRYPTION_KEY" in proc.stderr, (
        f"отказ с коротким ключом не назвал причину: {proc.stderr[-500:]}"
    )


def test_app_starts_with_valid_key():
    """Контроль: с валидным ключом import app.main проходит — отказ
    вызван именно ключом, а не чем-то попутно."""
    proc = _run_import(
        {
            "APP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DATABASE_URL": "sqlite+aiosqlite://",
        }
    )
    assert proc.returncode == 0, f"валидный ключ не принял: {proc.stderr[-500:]}"
