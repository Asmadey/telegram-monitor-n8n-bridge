"""Зависимости роутеров (задача 2.3): require_user, require_admin (позже).

Порт before_action :require_authentication: зависимость вешается на роутер
целиком, а не на каждый эндпоинт руками — забыть закрыть эндпоинт
невозможно, можно только забыть открыть (это заметно сразу).
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Session, User
from app.security.sessions import SESSION_COOKIE, resolve_session


async def require_session(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Session:
    """Аноним — 401 без объяснений: не раскрываем, чем именно не подошла cookie.

    Возвращает строку сессии (logout-у нужен её id, а не юзер).
    """
    raw = request.cookies.get(SESSION_COOKIE)
    session = await resolve_session(db, raw) if raw else None
    if session is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return session


async def require_user(session: Session = Depends(require_session)) -> User:
    return session.user


async def require_admin(user: User = Depends(require_user)) -> User:
    """Порт admin/base_controller.rb. 403 для не-админа легален: админка —
    свой ресурс, её существование не секрет. Правило «на чужих ресурсах
    404, не 403» — про данные тенантов, не про роль."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user