"""автоочистка на тенанта + уникальность public_id в пределах пользователя

Revision ID: 0005_cleanup_and_public_id
Revises: 0004_identities
Create Date: 2026-09-04

Две правки, обе следуют из мульти-тенанта.

1. Автоочистка базы (server.py:484) в монолите хранила настройки в общей
   таблице settings — одна на весь сервис. Сроки хранения данных не могут
   быть общими: это выбор владельца данных. Три колонки переезжают в
   integrations, где уже живёт остальная конфигурация тенанта.

2. monitors.public_id был уникален ГЛОБАЛЬНО. Старые id из SQLite — это
   имена каналов вроде `theyseeku`, поэтому при переносе данных второго
   пользователя его канал молча не вставлялся: `ON CONFLICT DO NOTHING`
   по глобальному ключу. Тихая потеря данных хуже падения — её не видно.
   Уникальность становится парой (user_id, public_id).
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_cleanup_and_public_id"
down_revision = "0004_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column(
            "cleanup_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "integrations",
        sa.Column("cleanup_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "integrations",
        sa.Column("cleanup_last_run", sa.DateTime(timezone=True), nullable=True),
    )

    # уникальность заведена индексом (0001), поэтому индекс же и меняем
    op.drop_index(op.f("uq_monitors_public_id"), table_name="monitors")
    op.create_index(
        op.f("uq_monitors_user_public_id"),
        "monitors",
        ["user_id", "public_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_monitors_user_public_id"), table_name="monitors")
    op.create_index(
        op.f("uq_monitors_public_id"), "monitors", ["public_id"], unique=True
    )
    op.drop_column("integrations", "cleanup_last_run")
    op.drop_column("integrations", "cleanup_days")
    op.drop_column("integrations", "cleanup_enabled")
