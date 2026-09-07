"""Выборка сообщений канала (задача 4.7 PLAN.md, порт server.py:675).

fetch_chat_messages оригинала живёт на глобальном синглтоне `client` и
HTTPException; порт принимает КЛИЕНТА ИНЪЕКЦИЕЙ (в бою — клиент из пула
воркера, 3.5; в тестах — фейк с iter_messages) и уже разрешённый entity
(разрешение цели — отдельный шаг вызывающего, он же владеет клиентом).

ДЕФЕКТ ОРИГИНАЛА (С23, server.py:706), исправленный портом: `break` на
первом сообщении старее cutoff. Закреплённое сообщение старее cutoff
приходит РАНЬШЕ свежих постов — break терял ВСЁ новое канала; здесь —
`continue` (пропустить старьё и читать дальше), объём ограничен limit.
"""

import datetime


def build_post_url(entity, msg_id: int) -> str:
    """Порт server.py:665 — ссылка на пост по username канала."""
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    entity_id = getattr(entity, "id", 0)
    if entity_id:
        clean_id = str(entity_id).replace("-100", "").replace("-", "")
        return f"https://t.me/c/{clean_id}/{msg_id}"
    return ""


async def fetch_channel_messages(
    client,
    entity,
    *,
    limit: int = 20,
    offset_hours: int | None = None,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """Выбрать до `limit` свежих сообщений канала не старее `offset_hours`.

    Итерация ограничена `limit` (iter_messages не читает канал
    бесконечно); сообщения без текста пропускаются (порт оригинала).
    """
    time_cutoff = None
    if offset_hours:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        time_cutoff = now - datetime.timedelta(hours=offset_hours)

    messages: list[dict] = []
    async for msg in client.iter_messages(entity, limit=limit):
        if time_cutoff and msg.date and msg.date < time_cutoff:
            # С23 (4.7): закреп старее cutoff приходит РАНЬШЕ свежих
            # постов — break терял бы ВСЁ новое. Пропускаем и читаем
            # дальше; объём выборки ограничен limit у iter_messages.
            continue

        text = (msg.text or "").strip()
        # чистые картинки/стикеры без описания не интересуют монитор
        if not text or text == "📎 [Медиа/Вложение]":
            continue

        sender = await msg.get_sender()
        sender_name = (
            "Вы"
            if msg.out
            else (
                getattr(sender, "first_name", "")
                or getattr(sender, "title", "")
                or (getattr(entity, "title", "") or "")
            )
        )
        date_str = msg.date.isoformat() if msg.date else ""

        # подсчёт реакций у поста
        reactions_count = 0
        reactions_details: list[dict] = []
        if getattr(msg, "reactions", None) and getattr(msg.reactions, "results", None):
            for r in msg.reactions.results:
                count = getattr(r, "count", 0)
                reactions_count += count
                emoticon = getattr(getattr(r, "reaction", None), "emoticon", "")
                reactions_details.append({"emoji": emoticon, "count": count})

        messages.append(
            {
                "id": msg.id,
                "date": date_str,
                "sender": sender_name,
                "sender_id": msg.sender_id,
                "is_outgoing": msg.out,
                "text": text,
                "has_media": bool(msg.media),
                "views": getattr(msg, "views", None),
                "forwards": getattr(msg, "forwards", None),
                "reactions_count": reactions_count,
                "reactions": reactions_details,
                "post_url": build_post_url(entity, msg.id),
            }
        )
    return messages
