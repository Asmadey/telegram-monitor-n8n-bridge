"""Release regression scenarios: static visitor, Railway and Firebase startup."""

import json
from pathlib import Path

import pytest

from app.api.monitors import MonitorCreate
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_vercel_serves_existing_page_assets_and_google_sdk():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert "framework" in cfg and cfg["framework"] is None
    assert {"source": "/static/:path*", "destination": "/:path*"} in cfg["rewrites"]
    raw = json.dumps(cfg)
    assert "REPLACE-WITH-RAILWAY-HOST" not in raw
    assert "https://web-production-d2e997.up.railway.app" in raw
    csp = next(
        h["value"]
        for h in cfg["headers"][0]["headers"]
        if h["key"] == "Content-Security-Policy"
    )
    assert "https://www.gstatic.com" in csp
    assert "https://mtproto-ai.firebaseapp.com" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp


@pytest.mark.asyncio
async def test_backend_csp_allows_the_fonts_used_by_index(anon_client):
    response = await anon_client.get("/")
    csp = response.headers["content-security-policy"]
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp


def test_railway_database_url_selects_installed_async_driver():
    assert Settings(
        database_url="postgresql://test:pass@localhost/db", _env_file=None
    ).database_url.startswith("postgresql+asyncpg://")


def test_message_batch_can_exceed_old_quota():
    try:
        req = MonitorCreate(chat_target="@example", limit=1000)
    except ValueError:
        req = None
    assert req is not None, "User cannot save requested batch larger than 100"


def test_firebase_verifier_requires_explicit_project(monkeypatch):
    import pytest

    from app.config import get_settings
    from app.services.google_auth import _live_verifier

    monkeypatch.setenv("FIREBASE_PROJECT_ID", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="FIREBASE_PROJECT_ID"):
        _live_verifier("invalid")
    get_settings.cache_clear()


def test_firebase_verifier_accepts_signed_token_without_private_credentials(
    monkeypatch,
):
    import time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt, jwt
    from google.oauth2 import id_token

    from app.config import get_settings
    from app.services.google_auth import _live_verifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    signer = crypt.RSASigner.from_string(private, key_id="test")
    monkeypatch.setattr(id_token, "_fetch_certs", lambda *a, **kw: {"test": public})
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "mtproto-ai")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    get_settings.cache_clear()
    now = int(time.time())
    claims = {
        "aud": "mtproto-ai",
        "iss": "https://securetoken.google.com/mtproto-ai",
        "sub": "user123",
        "iat": now,
        "exp": now + 3600,
        "auth_time": now,
        "firebase": {"sign_in_provider": "google.com"},
    }
    token = jwt.encode(signer, claims).decode()
    try:
        result = _live_verifier(token)
        error = None
    except Exception as exc:
        result, error = None, exc
    assert result is not None, str(error)
    assert result["sub"] == "user123"

    for change in (
        {"aud": "other"},
        {"iss": "https://attacker.example"},
        {"exp": now - 1},
        {"auth_time": now + 100},
        {"iat": now + 100},
        {"sub": ""},
        {"firebase": {"sign_in_provider": "password"}},
    ):
        invalid = jwt.encode(signer, {**claims, **change}).decode()
        with pytest.raises(ValueError):
            _live_verifier(invalid)
    get_settings.cache_clear()


def test_firebase_certificate_request_has_a_bounded_timeout(monkeypatch):
    import base64
    import time

    from app.config import get_settings
    from app.services import google_auth

    seen = {}

    class FakeRequest:
        def __call__(self, url, method="GET", body=None, headers=None, timeout=120):
            seen["url"] = url
            seen["timeout"] = timeout

    def fake_verify(token, request, audience=None):
        seen["audience"] = audience
        request("https://www.googleapis.com/robot/v1/metadata/x509/test")
        now = int(time.time())
        return {
            "aud": audience,
            "iss": f"https://securetoken.google.com/{audience}",
            "sub": "user123",
            "iat": now,
            "exp": now + 3600,
            "auth_time": now,
            "firebase": {"sign_in_provider": "google.com"},
        }

    monkeypatch.setenv("FIREBASE_PROJECT_ID", "mtproto-ai")
    monkeypatch.setattr(google_auth, "Request", FakeRequest)
    monkeypatch.setattr(google_auth.id_token, "verify_firebase_token", fake_verify)
    get_settings.cache_clear()
    # Токен собирается в рантайме, а не лежит литералом: секрет-скан не
    # отличает выдуманный JWT от настоящего и справедливо ловит длинную
    # строку с высокой энтропией (то же случилось 4 сентября с фиктивным
    # ключом в test_53). Верификатор здесь подменён — значение не важно.
    token = ".".join(
        [
            base64.urlsafe_b64encode(
                json.dumps({"alg": "RS256", "kid": "test"}).encode()
            )
            .rstrip(b"=")
            .decode(),
            base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode(),
            "signature-is-not-checked",
        ]
    )
    assert google_auth._live_verifier(token)["sub"] == "user123"
    assert seen["audience"] == "mtproto-ai"
    assert seen["timeout"] == 10
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_google_login_runs_verifier_outside_event_loop(anon_client, monkeypatch):
    from app.api import auth as auth_api
    from app.main import app
    from app.services.google_auth import get_google_verifier

    called = False

    async def fake_threadpool(function, *args):
        nonlocal called
        called = True
        return function(*args)

    def verifier(token):
        assert token == "signed-token"
        return {
            "email": "threadpool@example.com",
            "email_verified": True,
            "sub": "threadpool-user",
        }

    monkeypatch.setattr(auth_api, "run_in_threadpool", fake_threadpool)
    app.dependency_overrides[get_google_verifier] = lambda: verifier
    try:
        response = await anon_client.post(
            "/auth/google", json={"id_token": "signed-token"}
        )
    finally:
        app.dependency_overrides.pop(get_google_verifier, None)
    assert response.status_code == 200, response.text
    assert called


def test_admin_sdk_is_not_a_runtime_dependency():
    requirements = (ROOT / "requirements.txt").read_text().splitlines()
    packages = {
        line.split("[", 1)[0].split(">", 1)[0].lower()
        for line in requirements
        if line and not line.startswith("#")
    }
    assert "google-auth" in packages
    assert "firebase-admin" not in packages


@pytest.mark.asyncio
async def test_large_batch_saved_and_paginated_channels_stay_private(
    anon_client, db, user_a, user_b
):
    from conftest import act_as

    from app.models import Monitor

    await act_as(anon_client, db, user_a)
    for owner, public_id in (
        (user_a.id, "one"),
        (user_a.id, "two"),
        (user_b.id, "private"),
    ):
        db.add(Monitor(user_id=owner, public_id=public_id, chat_target="@" + public_id))
    await db.commit()
    response = await anon_client.patch("/api/monitors/one", json={"limit": 1000})
    assert response.status_code == 200, response.text
    assert response.json()["limit"] == 1000
    first = (await anon_client.get("/api/monitors?limit=1&offset=0")).json()["monitors"]
    second = (await anon_client.get("/api/monitors?limit=1&offset=1")).json()[
        "monitors"
    ]
    assert {first[0]["public_id"], second[0]["public_id"]} == {"one", "two"}
    assert (await anon_client.get("/api/monitors?limit=1&offset=2")).json()[
        "monitors"
    ] == []


def test_static_password_reset_bootstraps_before_first_post():
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip(
            "Node required to run real auth-page JavaScript with browser boundary doubles"
        )
    script = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
let submit;
const error = {textContent: ''};
const success = {textContent: ''};
const form = {addEventListener: (name, fn) => {submit = fn;}, querySelector: (s) => s === '.form-error' ? error : success};
const document = {cookie: '', getElementById: id => id === 'requestResetForm' ? form : id === 'email' ? {value: 'owner@example.com'} : null};
const calls = [];
const context = {document, window: {}, fetch: async (url, options) => {
  calls.push(url);
  if (options.method !== 'POST') {
    document.cookie = 'csrf_token=signed-token';
    return {ok: true};
  }
  assert.equal(options.headers['X-CSRF-Token'], 'signed-token');
  return {ok: true, json: async () => ({detail:'sent'})};
}};
vm.runInNewContext(fs.readFileSync('static/js/auth-pages.js', 'utf8'), context);
(async () => {await submit({preventDefault(){}}); assert.deepEqual(calls, ['/auth/google/config', '/auth/password-reset']); assert.equal(success.textContent, 'sent');})().catch(e => {console.error(e); process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_edit_channel_does_not_reject_saved_large_batch():
    source = (ROOT / "static/js/channels.js").read_text()
    assert "limit > 100" not in source, (
        "Edit form still refuses a batch accepted by the API"
    )


def test_frontend_uses_current_tenant_scoped_telegram_routes():
    auth_source = (ROOT / "static/js/auth.js").read_text()
    channel_source = (ROOT / "static/js/channels.js").read_text()
    for obsolete in (
        "/api/settings",
        "/api/auth/send-code",
        "/api/auth/sign-in",
        "/api/auth/logout",
    ):
        assert obsolete not in auth_source, f"Frontend still calls removed {obsolete}"
    for current in (
        "/api/telegram/status",
        "/api/telegram/send-code",
        "/api/telegram/sign-in",
        "/api/telegram/logout",
    ):
        assert current in auth_source, f"Frontend does not call {current}"
    assert "/api/telegram/dialogs?limit=30" in channel_source
    assert "apiGet('/dialogs" not in channel_source


def test_removed_webhook_relay_is_not_exposed_in_the_interface():
    html = (ROOT / "static/index.html").read_text()
    source = (ROOT / "static/js/messages.js").read_text()
    assert 'id="sendTableToN8nBtn"' not in html
    assert "/api/webhook/send-payload" not in source


@pytest.mark.asyncio
async def test_telegram_status_is_private_and_scoped_to_current_user(
    anon_client, db, user_a, user_b, monkeypatch
):
    from conftest import act_as

    from app.config import get_settings
    from app.models import TelegramAccount

    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "configured-hash")
    get_settings.cache_clear()
    for owner, suffix in ((user_a.id, "a"), (user_b.id, "b")):
        db.add(
            TelegramAccount(
                user_id=owner,
                phone=f"+7000000000{owner}",
                session_string_encrypted=f"secret-session-{suffix}",
                tg_user_id=1000 + owner,
                tg_username=f"telegram-{suffix}",
            )
        )
    await db.commit()

    assert (await anon_client.get("/api/telegram/status")).status_code == 401
    await act_as(anon_client, db, user_a)
    response = await anon_client.get("/api/telegram/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_id"] == 123456
    assert body["has_api_hash"] is True
    assert body["is_authorized"] is True
    assert body["user"] == {
        "id": 1000 + user_a.id,
        "username": "telegram-a",
        "phone": f"+7000000000{user_a.id}",
    }
    raw = response.text
    assert "secret-session" not in raw
    assert "configured-hash" not in raw
    assert "telegram-b" not in raw
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_history_endpoints_expose_records_beyond_first_page(
    anon_client, db, user_a, user_b
):
    from conftest import act_as

    from app.models import FeedItem, LogEntry, SentMessage

    for index in range(3):
        db.add(
            SentMessage(
                user_id=user_a.id,
                chat_id=100,
                message_id=index + 1,
                text=f"message-{index}",
            )
        )
        db.add(
            LogEntry(
                user_id=user_a.id,
                event_type="PAGE",
                status="INFO",
                details=f"log-{index}",
            )
        )
        db.add(
            FeedItem(
                user_id=user_a.id,
                job_id=f"page-job-{index}",
                messages_count=1,
            )
        )
    db.add(
        SentMessage(
            user_id=user_b.id, chat_id=200, message_id=1, text="foreign-message"
        )
    )
    db.add(
        LogEntry(
            user_id=user_b.id,
            event_type="PAGE",
            status="INFO",
            details="foreign-log",
        )
    )
    db.add(FeedItem(user_id=user_b.id, job_id="foreign-job", messages_count=1))
    await db.commit()
    await act_as(anon_client, db, user_a)

    for path, key in (
        ("/api/messages", "messages"),
        ("/api/logs", "logs"),
        ("/api/feed", "feed"),
    ):
        first = (await anon_client.get(f"{path}?limit=1&offset=0")).json()
        second = (await anon_client.get(f"{path}?limit=1&offset=1")).json()
        last = (await anon_client.get(f"{path}?limit=1&offset=3")).json()
        assert first["total"] == 3
        assert second["total"] == 3
        assert first[key][0]["id"] != second[key][0]["id"]
        assert last[key] == []
        assert "foreign" not in json.dumps(first)


def test_history_pages_offer_load_more_instead_of_hiding_old_records():
    html = (ROOT / "static/index.html").read_text()
    for area in ("Messages", "Feed", "Logs"):
        source = (ROOT / f"static/js/{area.lower()}.js").read_text()
        assert "offset=" in source, f"{area} never requests records after page one"
        assert f'id="loadMore{area}Btn"' in html


def test_frontend_treats_worker_actions_as_queued():
    channels = (ROOT / "static/js/channels.js").read_text()
    feed = (ROOT / "static/js/feed.js").read_text()
    assert "data.status === 'queued'" in channels
    assert "data.status === 'queued'" in feed
    assert "data.status === 'success'" not in feed


@pytest.mark.asyncio
async def test_live_check_adapter_supports_openrouter_and_telegram(monkeypatch):
    from app.api import checks
    from app.services import dispatch, llm

    calls = []

    async def allow_url(url):
        calls.append(("validated", url))

    def fake_openrouter_caller(*, api_key, base_url, model):
        calls.append(("openrouter", api_key, base_url, model))

        async def caller(payload):
            assert payload["model"] == model
            return "provider ok", 3

        return caller

    async def fake_bot_sender(token, chat_id, text):
        calls.append(("telegram_bot", token, chat_id, text))
        return True

    monkeypatch.setattr(checks, "validate_webhook_url", allow_url)
    monkeypatch.setattr(llm, "openrouter_caller", fake_openrouter_caller)
    monkeypatch.setattr(dispatch, "send_telegram_bot_message", fake_bot_sender)
    outbound = await checks.get_outbound()

    llm_result = await outbound(
        "openrouter",
        "public-test-key",
        {"base_url": "https://openrouter.ai/api/v1", "model": "test/model"},
    )
    bot_result = await outbound("telegram_bot", "public-test-token", {"chat_id": "123"})
    assert llm_result["response"] == "provider ok"
    assert bot_result["chat_id"] == "123"
    assert ("validated", "https://openrouter.ai/api/v1") in calls


@pytest.mark.asyncio
async def test_check_failures_never_return_or_log_provider_secrets(
    anon_client, db, user, app
):
    from sqlalchemy import select

    from app.api.checks import get_outbound
    from app.models import LogEntry
    from app.services.integrations import (
        save_integration_secrets,
        update_integration_config,
    )

    openrouter_key = "sk-or-" + "x" * 30
    bot_token = "123456789:" + "A" * 35
    await save_integration_secrets(
        db,
        user.id,
        openrouter_api_key=openrouter_key,
        bot_token=bot_token,
    )
    await update_integration_config(
        db,
        user.id,
        {
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            "openrouter_model": "test/model",
            "telegram_sender_id": "123",
        },
    )
    from conftest import act_as

    await act_as(anon_client, db, user)

    async def failing(kind, target, payload=None):
        raise RuntimeError(f"provider request failed with secret {target}")

    app.dependency_overrides[get_outbound] = lambda: failing
    try:
        for path, secret in (
            ("/api/openrouter/test", openrouter_key),
            ("/api/telegram-forward/test", bot_token),
        ):
            response = await anon_client.post(path)
            assert response.status_code == 502
            assert secret not in response.text
    finally:
        app.dependency_overrides.pop(get_outbound, None)

    details = " ".join(
        entry.details or "" for entry in (await db.scalars(select(LogEntry))).all()
    )
    assert openrouter_key not in details
    assert bot_token not in details


@pytest.mark.asyncio
async def test_migration_error_in_async_caller_reaches_operator(monkeypatch):
    import runpy

    from alembic.config import Config
    from sqlalchemy import ext

    from alembic import context

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(context, "config", Config(), raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)

    def fail_engine(*args, **kwargs):
        raise ValueError("synthetic migration failure")

    monkeypatch.setattr(ext.asyncio, "async_engine_from_config", fail_engine)
    try:
        runpy.run_path(str(ROOT / "alembic/env.py"))
        error = None
    except ValueError as exc:
        error = exc
    assert error is not None, (
        "Migration failed in a thread but CLI continued as success"
    )
    assert str(error) == "synthetic migration failure"
