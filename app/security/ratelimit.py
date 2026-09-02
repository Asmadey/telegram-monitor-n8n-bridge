"""Rate limiting (задача 2.7).

slowapi — для per-IP лимитов (login/signup: 10/3 мин — брутфорс). Для
password-reset лимит на пару IP+email: slowapi-декоратор не видит тело
запроса, поэтому свой скользящий интервал in-memory (5/час). Политика
send-code (3/час на пользователя) — важнейшая из таблицы плана: Telegram
отвечает на спам кодов FloodWaitError и может ограничить аккаунт
КЛИЕНТА на дни; вешается на эндпоинт при его переносе из server.py
(задача 3.3), чтобы не редактировать server.py.

In-memory хранилище живёт в процессе: на Railway воркер пока один.
При масштабировании — заменить на Redis (интерфейс allow() не изменится).
"""
import time
from collections import defaultdict, deque

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

TELEGRAM_SEND_CODE_LIMIT = "3/hour"  # на пользователя; применяется в задаче 3.3
PASSWORD_RESET_LIMIT = 5
PASSWORD_RESET_WINDOW = 3600.0  # час

_buckets: dict[str, deque[float]] = defaultdict(deque)


def allow(bucket: str, limit: int, window: float) -> bool:
    """True — попытка прошла и записана; False — лимит исчерпан (бросайте 429)."""
    now = time.monotonic()
    queue = _buckets[bucket]
    while queue and queue[0] <= now - window:
        queue.popleft()
    if len(queue) >= limit:
        return False
    queue.append(now)
    return True


def reset_all() -> None:
    """Сброс in-memory состояния (между тестами)."""
    _buckets.clear()
    limiter.reset()