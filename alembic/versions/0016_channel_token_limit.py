"""Потолок расхода токенов на канал (задача 13.5).

Задача 12.7 показала, какой канал жжёт бюджет; ограничить его было нечем —
потолок один на тенанта, и при его достижении AI выключается целиком.

Ревизия только ДОБАВЛЯЕТ колонку со значением по умолчанию: миграции идут до
старта нового кода, старый процесс о ней не знает, окна отказа нет.

Ноль — «без потолка». NOT NULL DEFAULT 0, а не NULL: «не задано» и «ноль
токенов» в запросах выглядели бы одинаково, а значат противоположное.
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_channel_token_limit"
down_revision = "0015_llm_usage_slices"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monitor_channels",
        sa.Column("token_limit", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
