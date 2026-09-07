"""Ключи приложения MTProto принадлежат пользователю (открытый вопрос №1).

Отдельная таблица, а не колонки в telegram_accounts: ключи вносятся ДО
подключения аккаунта и переживают отключение.
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_telegram_credentials"
down_revision = "0007_legacy_import_rows"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("api_id", sa.BigInteger(), nullable=False),
        sa.Column("api_hash_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_telegram_credentials_user_id", "telegram_credentials", ["user_id"]
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
