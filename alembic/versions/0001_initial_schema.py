"""initial schema — многотенантная схема задачи 1.3

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-01

Написана вручную (Bash был недоступен для `alembic revision --autogenerate`),
сверена с app/models построчно: 10 таблиц, user_id NOT NULL + индекс
у каждой тезисной таблицы, UNIQUE(user_id, chat_id, message_id).
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users — первым: на него ссылаются все FK
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"])

    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("session_string_encrypted", sa.Text(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(
        op.f("ix_telegram_accounts_user_id"),
        "telegram_accounts",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "tg_auth_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("phone_code_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_tg_auth_attempts_user_id"), "tg_auth_attempts", ["user_id"])

    op.create_table(
        "monitors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_target", sa.String(length=255), nullable=False),
        sa.Column("chat_title", sa.String(length=512), nullable=True),
        sa.Column("chat_username", sa.String(length=255), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("limit_count", sa.Integer(), nullable=False),
        sa.Column("offset_hours", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_message_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_monitors_user_id"), "monitors", ["user_id"])
    op.create_index(op.f("uq_monitors_public_id"), "monitors", ["public_id"], unique=True)

    op.create_table(
        "sent_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender", sa.String(length=512), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("post_url", sa.String(length=512), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reactions_count", sa.Integer(), nullable=False),
        sa.Column("forwards", sa.Integer(), nullable=False),
        sa.Column("has_media", sa.Boolean(), nullable=False),
        sa.Column("reactions_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("user_id", "chat_id", "message_id"),
    )
    op.create_index(op.f("ix_sent_messages_user_id"), "sent_messages", ["user_id"])

    op.create_table(
        "feed_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_title", sa.String(length=512), nullable=True),
        sa.Column("chat_username", sa.String(length=255), nullable=True),
        sa.Column("messages_count", sa.Integer(), nullable=False),
        sa.Column("ai_analysis", sa.Text(), nullable=True),
        sa.Column("raw_messages_json", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_feed_items_user_id"), "feed_items", ["user_id"])
    op.create_index(op.f("uq_feed_items_job_id"), "feed_items", ["job_id"], unique=True)

    op.create_table(
        "logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("chat_title", sa.String(length=512), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("messages_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_logs_user_id"), "logs", ["user_id"])

    op.create_table(
        "integrations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_bot_token_encrypted", sa.Text(), nullable=False),
        sa.Column("telegram_sender_id", sa.String(length=64), nullable=False),
        sa.Column("telegram_forward_enabled", sa.Boolean(), nullable=False),
        sa.Column("openrouter_api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("openrouter_base_url", sa.String(length=255), nullable=False),
        sa.Column("openrouter_model", sa.String(length=128), nullable=False),
        sa.Column("openrouter_enabled", sa.Boolean(), nullable=False),
        sa.Column("webhook_url_encrypted", sa.Text(), nullable=False),
        sa.Column("auto_webhook_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(
        op.f("ix_integrations_user_id"), "integrations", ["user_id"], unique=True
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_jobs_user_id"), "jobs", ["user_id"])


def downgrade() -> None:
    # в обратном порядке ссылок
    op.drop_table("jobs")
    op.drop_table("integrations")
    op.drop_table("logs")
    op.drop_table("feed_items")
    op.drop_table("sent_messages")
    op.drop_table("monitors")
    op.drop_table("tg_auth_attempts")
    op.drop_table("telegram_accounts")
    op.drop_table("sessions")
    op.drop_table("users")