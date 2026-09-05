"""Фаза 6 — вход через Google (Firebase), PLAN.md раздел 9.

Firebase — провайдер идентичности, а НЕ система сессий: verify_id_token
проверяет подпись/aud/exp, пользователь ищется или создаётся в СВОЕЙ
таблице, сессия — своя cookie (отзывается блокировкой юзера, работает
админка; GitHub/Apple добавятся без переписывания аутентификации).

Верификатор — инъектируемая зависимость (как Telethon-клиент в 3.3):
тесты подменяют её фейком, живой Firebase в CI не нужен.

Красные случаи из плана:
- невалидный токен → 401;
- валидный токен НОВОГО пользователя → создание + сессия;
- валидный токен с email СУЩЕСТВУЮЩЕГО пользователя → ПРИВЯЗКА
  (строка identities), а не второй аккаунт;
- подделанный токен с чужим aud → 401.

Сверх плана, по той же линии безопасности:
- email_verified=False → 401: иначе идентичность привязывается к адресу,
  который Google за пользователем НЕ подтвердил (перехват чужого ящика);
- uid уже связан с ДРУГИМ юзером при другом email → 401: молча
  перетасовывать владельца идентичности нельзя.
"""

import pathlib

import pytest
from sqlalchemy import select

from app.models import User

ROOT = pathlib.Path(__file__).resolve().parents[1]

# «токены» фейка: верификатор видит строку, по ней решает, валидна ли
FAKE_CLAIMS = {
    "valid-new": {
        "email": "google-user@example.com",
        "email_verified": True,
        "sub": "google-uid-new",
    },
    # email существующего юзера (fixture user_a) — привязка, не дубль
    "valid-existing": {
        "email": "tenant-a@example.com",
        "email_verified": True,
        "sub": "google-uid-existing",
    },
    "unverified-email": {
        "email": "not-confirmed@example.com",
        "email_verified": False,
        "sub": "google-uid-unverified",
    },
    # чужой aud / подпись — firebase_admin.raise InvalidIdTokenError;
    # фейк поднимает то же исключение контракта (любое)
}


def _fake_verify(token: str) -> dict:
    claims = FAKE_CLAIMS.get(token)
    if claims is None:
        raise ValueError("InvalidIdToken")  # невалидный/подделанный
    return claims


@pytest.fixture
def fake_google(anon_client):
    """Подмена верификатора (паттерн fake_tg из 3.3). Если сервисного
    модуля ещё нет (красная фаза) — не подменяем: POST /auth/google
    честно ответит 404, и поведенческие ассерты упадут сами, а не на
    ImportError фикстуры."""
    from app.main import app

    try:
        from app.services.google_auth import get_google_verifier
    except ImportError:
        yield False
        return
    app.dependency_overrides[get_google_verifier] = lambda: _fake_verify
    yield True
    app.dependency_overrides.pop(get_google_verifier, None)


# --------------------------------------------------------------------------
# Структурные
# --------------------------------------------------------------------------


def _identity_model():
    """Модель идентичности; в красной фазе (модели нет) падаем на
    assert, а не на ImportError."""
    from app import models

    cls = getattr(models, "UserIdentity", None)
    assert cls is not None, "app.models.UserIdentity не существует (Фаза 6)"
    return cls


def test_identity_model_exists():
    """identities (user_id, provider, provider_uid) — таблица связывания
    внешних идентичностей; (provider, provider_uid) уникальна: один
    Google-аккаунт не может быть связан с двумя юзерами. Модель —
    UserIdentity: имя Identity в модуле занято sqlalchemy.Identity
    (PK-конструктор), тень ломала бы mapped_column у следующих моделей."""
    from app import models
    from app.models import Base

    cls = getattr(models, "UserIdentity", None)
    assert cls is not None and issubclass(cls, Base), (
        "app.models.UserIdentity не существует (Фаза 6; sqlalchemy.Identity "
        "тень бытует в модуле — имя модели сознательно не UserIdentity?)"
    )
    cols = cls.__table__.columns
    assert {"user_id", "provider", "provider_uid"} <= set(cols.keys()), (
        "identities не несёт колонок связывания"
    )
    # таблица несёт UniqueConstraint-объекты (а не флаг column.unique):
    # isinstance, у объекта UniqueConstraint нет атрибута .unique
    from sqlalchemy import UniqueConstraint

    uniques = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in cls.__table__.constraints
        if isinstance(uc, UniqueConstraint)
    }
    assert ("provider", "provider_uid") in uniques, (
        "нет UNIQUE(provider, provider_uid) — идентичность можно задвоить"
    )


def test_migration_creates_identities():
    """Схема — только миграциями: у identities своя ревизия, цепочка
    неразрывна (0004 за 0003_chat_avatars)."""
    versions = ROOT / "alembic" / "versions"
    hits = [
        f
        for f in versions.glob("*.py")
        if '"identities"' in f.read_text(encoding="utf-8")
        and "create_table" in f.read_text(encoding="utf-8")
    ]
    assert hits, "нет ревизии, создающей identities"
    src = hits[0].read_text(encoding="utf-8")
    assert 'down_revision = "0003_chat_avatars"' in src, (
        "ревизия identities не встраивается за 0003_chat_avatars"
    )


def test_google_service_exposes_injectable_verifier():
    """Верификатор — зависимость FastAPI (подменяемая в тестах), а не
    зашитый вызов firebase_admin внутри эндпоинта."""
    import importlib

    try:
        mod = importlib.import_module("app.services.google_auth")
    except ImportError:
        mod = None
    assert mod is not None, "app/services/google_auth.py не существует"
    assert callable(getattr(mod, "get_google_verifier", None)), (
        "нет get_google_verifier — верификацию не подменить"
    )


# --------------------------------------------------------------------------
# Поведенческие
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_token_is_401(anon_client, db, fake_google):
    """Невалидный токен → 401 без объяснений (контракт верификатора:
    любое исключение → единый 401)."""
    r = await anon_client.post("/auth/google", json={"id_token": "garbage"})
    assert r.status_code == 401, (
        f"невалидный токен → {r.status_code}, должен 401 (404 = роута ещё нет)"
    )


@pytest.mark.asyncio
async def test_forged_aud_token_is_401(anon_client, db, fake_google):
    """Подделанный токен с чужим aud/firebase-admin отверг его — 401.
    Фейк поднимает то же исключение, что живой verify_id_token."""
    r = await anon_client.post("/auth/google", json={"id_token": "forged-aud"})
    assert r.status_code == 401, f"подделанный aud → {r.status_code}, должен 401"


@pytest.mark.asyncio
async def test_valid_token_creates_user_and_session(anon_client, db, fake_google):
    """Валидный токен нового пользователя: ОДИН аккаунт (без пароля —
    password_hash NULL), строка identities, рабочая своя cookie-сессия."""
    UserIdentity = _identity_model()

    r = await anon_client.post("/auth/google", json={"id_token": "valid-new"})
    assert r.status_code == 200, f"валидный токен нового юзера → {r.status_code}"
    body = r.json()
    assert body["email"] == "google-user@example.com", f"ответ не пользователь: {body}"

    created = (
        (await db.scalars(select(User).where(User.email == "google-user@example.com")))
        .unique()
        .all()
    )
    assert len(created) == 1, "создано больше одного аккаунта на email"
    assert created[0].password_hash is None, (
        "Google-юзер без пароля: password_hash обязан быть NULL"
    )

    idents = (
        (
            await db.scalars(
                select(UserIdentity).where(
                    UserIdentity.provider_uid == "google-uid-new"
                )
            )
        )
        .unique()
        .all()
    )
    assert len(idents) == 1 and idents[0].user_id == created[0].id, (
        "нет строки identities — при следующем входе юзер создастся заново"
    )

    # СВОЯ сессия (не Firebase): /auth/me по выданной cookie видит юзера
    me = await anon_client.get("/auth/me")
    assert me.status_code == 200, "cookie-сессия не выдана"
    assert me.json()["email"] == "google-user@example.com"


@pytest.mark.asyncio
async def test_existing_email_links_not_duplicates(
    anon_client, db, user_a, fake_google
):
    """Валидный токен с email существующего пользователя → ПРИВЯЗКА
    (строка identities у СУЩЕГО юзера), а не второй аккаунт."""
    UserIdentity = _identity_model()

    r = await anon_client.post("/auth/google", json={"id_token": "valid-existing"})
    assert r.status_code == 200, f"вход с существующим email → {r.status_code}"

    same_email = (
        (await db.scalars(select(User).where(User.email == user_a.email)))
        .unique()
        .all()
    )
    assert len(same_email) == 1, "создан дубль аккаунта вместо привязки"
    assert same_email[0].id == user_a.id

    idents = (
        (
            await db.scalars(
                select(UserIdentity).where(
                    UserIdentity.provider_uid == "google-uid-existing"
                )
            )
        )
        .unique()
        .all()
    )
    assert len(idents) == 1 and idents[0].user_id == user_a.id, (
        "идентичность не связана с существующим юзером"
    )


@pytest.mark.asyncio
async def test_unverified_email_is_401(anon_client, db, fake_google):
    """email_verified=False → 401: Google не подтвердил адрес за юзером —
    привязывать идентичность к неподтверждённому email нельзя (иначе
    вход через Google становится обходом владения ящиком)."""
    r = await anon_client.post("/auth/google", json={"id_token": "unverified-email"})
    assert r.status_code == 401, f"неподтверждённый email → {r.status_code}, должен 401"
    got = (
        (
            await db.scalars(
                select(User).where(User.email == "not-confirmed@example.com")
            )
        )
        .unique()
        .all()
    )
    assert not got, "аккаунт создан по неподтверждённому email"


@pytest.mark.asyncio
async def test_uid_owned_by_other_user_is_401(anon_client, db, user_b, fake_google):
    """uid уже связан с ДРУГИМ юзером, а email в токене — другой: молча
    перетасовывать владельца идентичности нельзя → 401 (без объяснений,
    чей это uid — существование не раскрываем)."""
    UserIdentity = _identity_model()

    db.add(
        UserIdentity(
            user_id=user_b.id, provider="google", provider_uid="google-uid-conflict"
        )
    )
    await db.commit()

    claims_token = "valid-conflict"  # uid чужой, email новый
    FAKE_CLAIMS[claims_token] = {
        "email": "someone-else@example.com",
        "email_verified": True,
        "sub": "google-uid-conflict",
    }
    r = await anon_client.post("/auth/google", json={"id_token": claims_token})
    assert r.status_code == 401, f"чужой uid → {r.status_code}, должен 401"
    still_b = (
        (
            await db.scalars(
                select(UserIdentity).where(
                    UserIdentity.provider_uid == "google-uid-conflict"
                )
            )
        )
        .unique()
        .all()
    )
    assert len(still_b) == 1 and still_b[0].user_id == user_b.id, (
        "владелец идентичности перезаписан"
    )
