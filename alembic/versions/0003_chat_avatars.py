"""chat_avatars — аватарки каналов отдельно от строк ленты (задача 5.4)

Revision ID: 0003_chat_avatars
Revises: 0002_llm_usage
Create Date: 2026-09-04

Написана вручную (как 0001/0002), сверена с app.models.ChatAvatar
построчно. Без user_id СОЗНАТЕЛЬНО: фото публичного канала одно на всех,
изоляция — на чтении (эндпоинт отдаёт bytes только юзеру, который
мониторит канал). Раньше photo_base64 лежала в каждой строке analysis_feed
и уезжала клиенту списком — мегабайты на каждый GET /api/feed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_chat_avatars"
down_revision = "0002_llm_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_avatars",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("image_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chat_avatars")
