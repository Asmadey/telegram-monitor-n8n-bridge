"""Роутер аутентификации: /auth/me, /auth/logout, signup, login (задачи 2.3–2.4).

Закрыто по умолчанию: защищённый `router` несёт Depends(require_user) на
РОУТЕРЕ — новый эндпоинт в нём защищён автоматически. signup и login —
явный opt-out, живут в `public_router` и в белом списке test_22.

Порт registrations_controller.rb / sessions_controller.rb (Rails-шаблон).
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_session, require_user
from app.models import Session, User
from app.security.password_reset import make_reset_token, resolve_reset_token
from app.security.passwords import hash_password, verify_password
from app.security.sessions import (
    clear_session_cookie,
    create_session,
    destroy_session,
    set_session_cookie,
)
from app.services.mailer import send_password_reset_email

router = APIRouter(dependencies=[Depends(require_user)])
public_router = APIRouter()

_MIN_PASSWORD = 8
_MAX_PASSWORD_BYTES = 72  # bcrypt больше не хеширует; hash_password откажется
_GENERIC_422 = "Регистрация не прошла: проверьте email и пароль"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "timezone": user.timezone,
        "is_admin": user.is_admin,
    }


class EmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _email_lower(cls, v: str) -> str:
        return v.strip().lower()


class AuthRequest(EmailRequest):
    password: str


def _password_rules(v: str) -> str:
    """Общие правила пароля (signup и сброс): 8..72 байт."""
    if len(v) < _MIN_PASSWORD:
        raise ValueError(f"пароль короче {_MIN_PASSWORD} символов")
    if len(v.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        # bcrypt молча усекает до 72 байт — хеширование откажется (задача 2.1)
        raise ValueError(f"пароль длиннее {_MAX_PASSWORD_BYTES} байт")
    return v


class SignupRequest(AuthRequest):
    timezone: str = "UTC"

    @field_validator("password")
    @classmethod
    def _signup_password(cls, v: str) -> str:
        return _password_rules(v)


class PasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password_rules(cls, v: str) -> str:
        return _password_rules(v)


async def _open_session(
    db: AsyncSession, request: Request, response: Response, user: User
) -> None:
    """Сессия в БД + подписанная cookie в ответ (и signup, и login)."""
    session = await create_session(
        db,
        user,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    set_session_cookie(response, session.id)


@router.get("/auth/me")
async def me(user: User = Depends(require_user)) -> dict[str, Any]:
    return _user_dict(user)


@router.post("/auth/logout")
async def logout(
    response: Response,  # FastAPI инжектит реальный ответ; default не нужен
    session: Session = Depends(require_session),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    await destroy_session(db, session.id)
    clear_session_cookie(response)
    return {"ok": True}


@public_router.post("/auth/signup")
async def signup(
    req: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Существующий email — 422 с той же формулировкой, что у прочих ошибок
    # валидации: ответ не должен позволять перебирать адреса.
    existing = await db.scalar(select(User).where(User.email == req.email))
    if existing is not None:
        raise HTTPException(status_code=422, detail=_GENERIC_422)
    try:
        user = User(
            email=req.email,
            password_hash=hash_password(req.password),
            timezone=req.timezone,
        )
        db.add(user)
        await db.commit()
    except ValueError:
        # hash_password отказал (72 байта) — на всякий случай тот же 422
        raise HTTPException(status_code=422, detail=_GENERIC_422) from None
    await _open_session(db, request, response, user)
    return _user_dict(user)


@public_router.post("/auth/login")
async def login(
    req: AuthRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Один и тот же 401 для «нет такого юзера» и «не тот пароль» — иначе
    # эндпоинт перечисляет зарегистрированные адреса.
    user = await db.scalar(select(User).where(User.email == req.email))
    if (
        user is None
        or user.password_hash is None
        or not verify_password(req.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    await _open_session(db, request, response, user)
    return _user_dict(user)


@public_router.post("/auth/password-reset")
async def request_password_reset(
    req: EmailRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    # Ответ одинаков для существующего и несуществующего адреса — иначе
    # эндпоинт перечисляет пользователей (passwords_controller.rb:create).
    user = await db.scalar(select(User).where(User.email == req.email))
    if user is not None:
        await send_password_reset_email(user.email, make_reset_token(user))
    return {
        "ok": True,
        "detail": "Если адрес зарегистрирован, письмо со ссылкой отправлено",
    }


@public_router.post("/auth/password-reset/confirm")
async def confirm_password_reset(
    req: PasswordResetConfirmRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    # 422 без объяснений: подделка, истёк, уже использован — не различаем
    user = await resolve_reset_token(db, req.token)
    if user is None:
        raise HTTPException(status_code=422, detail="Ссылка недействительна или устарела")
    user.password_hash = hash_password(req.new_password)
    await db.commit()
    # смена пароля убивает ВСЕ сессии пользователя (в т.ч. угнанные)
    await db.execute(delete(Session).where(Session.user_id == user.id))
    await db.commit()
    return {"ok": True}