"""Модели SQLAlchemy — многотенантная схема (задача 1.3 PLAN.md).

Двенадцать таблиц. Ключевое отличие от старой схемы: user_id NOT NULL + индекс
у каждой таблицы с данными тенанта — без индекса фильтр по пользователю
вырождается в full scan, без NOT NULL строка «без владельца» видна всем.

Секреты (MTProto-сессия, токены интеграций) хранятся только в
*_encrypted колонках; шифрование — задача 3.4.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


# BIGINT PK обязан автоинкрементиться на обеих СУБД. На SQLite автоинкремент
# даёт только INTEGER PRIMARY KEY (rowid-alias), на Postgres — Identity.
# Без этого вставка юзера падает «NOT NULL constraint failed: users.id»
# (поймано красным тестом test_21_sessions.py).
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    # по образцу Ruby/db/schema.rb:168

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    # email хранится в нижнем регистре; uniqueness проверяется приложением
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # nullable: вход только через Google OAuth
    password_hash: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class Session(Base):
    __tablename__ = "sessions"
    # по образцу Ruby/app/models/session.rb

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # lazy="joined": require_user трогает session.user после commit — ленивая
    # загрузка в async-контексте упадёт MissingGreenlet, поэтому сразу eager
    user: Mapped["User"] = relationship(lazy="joined")


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    # заменяет файл .session: сессия MTProto живёт в БД, шифрованная

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    # у пользователя один Telegram-аккаунт (сейчас; many — Phase 5+)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    # строка сессии Telethon — только зашифрованная (задача 3.4)
    session_string_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tg_username: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class TgAuthAttempt(Base):
    __tablename__ = "tg_auth_attempts"
    # заменяет глобальный auth_state-словарь в server.py

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    phone_code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    # публичный идентификатор для URL/интерфейса: BIGINT-PK наружу не светим
    public_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    chat_target: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(512))
    chat_username: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    limit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    offset_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    prompt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SentMessage(Base):
    __tablename__ = "sent_messages"
    # строгая дедупликация — теперь в разрезе тенанта

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender: Mapped[str | None] = mapped_column(String(512))
    text: Mapped[str | None] = mapped_column(Text)
    views: Mapped[int | None] = mapped_column(Integer)
    post_url: Mapped[str | None] = mapped_column(String(512))
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    reactions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forwards: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reactions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (UniqueConstraint("user_id", "chat_id", "message_id"),)


class FeedItem(Base):
    __tablename__ = "feed_items"
    # бывшая analysis_feed; photo_base64 вынесена в chat_avatars (задача 5.4)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_title: Mapped[str | None] = mapped_column(String(512))
    chat_username: Mapped[str | None] = mapped_column(String(255))
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_analysis: Mapped[str | None] = mapped_column(Text)
    raw_messages_json: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(128))
    delivery_status: Mapped[str | None] = mapped_column(String(32))


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(512))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)


class Integration(Base):
    __tablename__ = "integrations"
    # бывшая integrations_config, но БЕЗ CHECK (id = 1): по строке на пользователя.
    # Ключи — только в *_encrypted колонках (шифрование — задача 3.4).

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    telegram_bot_token_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    telegram_sender_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    telegram_forward_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    openrouter_api_key_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    openrouter_base_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://openrouter.ai/api/v1"
    )
    openrouter_model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="deepseek/deepseek-v4-flash"
    )
    openrouter_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    webhook_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auto_webhook_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class Job(Base):
    __tablename__ = "jobs"
    # ручной запуск задач из UI

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    # месячный счётчик токенов LLM на тенанта (задача 4.5): без него канал
    # с длинными постами и интервалом 15 минут — неограниченный счёт

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (UniqueConstraint("user_id", "period"),)


class ChatAvatar(Base):
    """Аватарка канала (задача 5.4 PLAN.md): раньше photo_base64 лежала
    в КАЖДОЙ строке ленты и уезжала клиенту списком — мегабайты на запрос.

    user_id здесь НЕТ и это не дыра: фото публичного канала одно на всех,
    дублировать его по тенантам — раздувание базы. Изоляция держится на
    чтении — эндпоинт отдаёт bytes только юзеру, который мониторит канал
    (иначе 404: существование канала произвольным клиентам не раскрываем)."""

    __tablename__ = "chat_avatars"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    image_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
