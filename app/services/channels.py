"""Канал источника: приведение адреса к одному виду.

Канал добавляется ссылкой, а `chat_id` появляется только при первом опросе
(так добавление не требует живого подключения к Telegram). Значит проверять
повтор приходится по адресу — и сравнивать строки буквально здесь нельзя:
Telegram считает `@forproducts`, `t.me/forproducts` и
`https://t.me/forproducts/` одним каналом, а `==` — четырьмя разными.

Ценой ошибки был не двойной опрос, а падение целого источника: ограничение
`(monitor_id, chat_id)` стреляет при ЗАПИСИ разрешённого `chat_id`, то есть
на прогоне, и исключение уносило с собой все остальные каналы (найдено
владельцем 2026-09-13 на источнике «ТОП-вакансии»).
"""

import re

_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_HOST = re.compile(r"^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", re.IGNORECASE)
# ссылка на ПОСТ публичного канала: имя и номер сообщения
_PUBLIC_POST = re.compile(r"^([A-Za-z0-9_]{4,32})/\d+$")

# Приглашение и закрытый канал — не имена. Хеш приглашения регистрозависим,
# а `c/<id>/<msg>` адресует чат по внутреннему номеру: сворачивать их к
# нижнему регистру или обрезать значит склеить разные каналы.
_NOT_A_USERNAME = ("+", "joinchat/", "c/")


def normalize_channel_target(raw: str) -> str:
    """Ключ сравнения для адреса канала — не для показа и не для запроса.

    Возвращается имя без `@` и хоста, в нижнем регистре. Адрес, который
    именем не является (приглашение, закрытый канал), возвращается как есть:
    его нельзя ни привести к регистру, ни укоротить.
    """
    value = (raw or "").strip()
    value = _SCHEME.sub("", value)
    value = _HOST.sub("", value)
    value = value.split("?", 1)[0].rstrip("/")
    value = value.lstrip("@")

    lowered = value.lower()
    if any(lowered.startswith(mark) for mark in _NOT_A_USERNAME):
        return value

    post = _PUBLIC_POST.match(value)
    if post:
        # ссылка на пост называет тот же канал, что и ссылка на канал
        return post.group(1).lower()
    return lowered


def clean_target(target: str) -> str | int:
    """Адрес канала в том виде, который понимает `get_entity`.

    Числовой id возвращается ЧИСЛОМ: строку `"-100…"` Telethon разбирает как
    имя пользователя или телефон и до канала не доходит вовсе. Ровно из-за
    этого источник владельца не разбирался ни разу (2026-09-14) — функция
    существовала, но её не звал никто с переезда 11.8.

    `+` в пригласительной ссылке сохраняется: это часть хеша, а не мусор.
    Прежняя версия его срезала, и приватный канал, добавленный единственным
    доступным способом, не разрешился бы никогда — `AbCdEf` не имя
    пользователя. Функция была мёртвой, поэтому беда не проявлялась.
    """
    target = (target or "").strip()
    if "t.me/" in target:
        target = target.split("t.me/")[-1].rstrip("/")
    if target.startswith("@"):
        target = target[1:]
    if target.lstrip("-").isdigit():
        try:
            return int(target)
        except ValueError:
            pass
    return target


# --------------------------------------------------------------------------
# Проверка канала (задача 13.6)
# --------------------------------------------------------------------------

# Исход проверки. NULL в колонке — «не проверяли», и это ТРЕТЬЕ состояние:
# пустое поле, читаемое как зелёный вердикт, — тот же класс ошибки, что
# «совпадений нет» против «не смогли посмотреть» (фаза 9).
CHECK_OK = "ok"
CHECK_NO_POSTS = "no_posts"
CHECK_DUPLICATE = "duplicate"
CHECK_NOT_FOUND = "not_found"
CHECK_NO_ACCESS = "no_access"
CHECK_ERROR = "error"

# Сколько последних сообщений смотрит проба. Единицы мало: последним постом
# бывает картинка без подписи, и здоровый канал объявлялся бы пустым. Много
# — это уже выборка, а не проба, и стоит времени на каждом канале.
CHECK_PROBE_LIMIT = 5


def describe_entity(entity) -> str:
    """Что нашлось по адресу: канал, супергруппа, группа, бот, личный чат.

    Человек добавляет ссылку и вправе узнать, что она привела не туда, куда
    он думал: `@name` бывает и каналом, и ботом, и живым человеком, а читаются
    они одинаково — молча и не тем.
    """
    if getattr(entity, "bot", False):
        return "бот"
    if getattr(entity, "broadcast", False):
        return "канал"
    if getattr(entity, "megagroup", False):
        return "супергруппа"
    if getattr(entity, "title", None):
        return "группа"
    return "личный чат"


def entity_name(entity) -> str:
    """Имя для показа: у канала — заголовок, у человека — имя или @username."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [
        str(getattr(entity, field, "") or "") for field in ("first_name", "last_name")
    ]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    return f"@{username}" if username else ""
