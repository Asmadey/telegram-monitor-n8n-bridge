"""Токен сброса пароля (задача 2.5) — порт generates_token_for из Rails.

Одноразовость без отдельной таблицы: в подпись включён слепок (sha256)
текущего password_hash. Подпись не шифрует — сам хеш в токен класть
нельзя, слепок можно. После смены пароля хеш меняется, слепок в токене
расходится с текущим — токен мёртв. TTL — час (max_age на проверке).
"""

import hashlib
from typing import Any, Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User

RESET_TTL = 3600  # секунд, план 2.5
_SALT = "password-reset"


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().secret_key
    if not secret:
        raise RuntimeError("SECRET_KEY не задан: токен сброса нечем подписать")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def _hash_tag(user: User) -> str:
    if not user.password_hash:
        return ""
    return hashlib.sha256(user.password_hash.encode("ascii")).hexdigest()


def make_reset_token(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "tag": _hash_tag(user)})


async def resolve_reset_token(db: AsyncSession, token: str) -> Optional[User]:
    """Любая беда (подделка, истёк, уже использован, юзер удалён) — None."""
    try:
        payload: Any = _serializer().loads(token, max_age=RESET_TTL)
        uid = int(payload["uid"])
        tag = str(payload["tag"])
    except (BadSignature, ValueError, KeyError, TypeError):
        return None
    user = await db.get(User, uid)
    if user is None or _hash_tag(user) != tag:
        return None
    return user
