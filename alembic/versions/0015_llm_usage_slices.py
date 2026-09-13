"""Разрез расхода токенов по источнику и каналу (задача 12.7).

`llm_usage` считает на тенанта, а решение об отключении принимается по
каналу. Разрез заведён ОТДЕЛЬНОЙ таблицей, а не колонками в `llm_usage`:
месячный гейт читает одну строку на (тенант, период) через
`scalar_one_or_none`, и строки по каналам в той же таблице уронили бы его
на второй — сломав защиту от неограниченного счёта ради отчёта.

Источник назван публичным идентификатором, а не внешним ключом на
`monitors`: ключ дал бы либо каскад (история трат исчезает вместе с
источником), либо отказ удаления. Пустая строка — расход вне источника,
`chat_id = 0` — сведение по каналам.

Ревизия только СОЗДАЁТ. Миграции поднимаются до старта нового кода, и
старый процесс о новой таблице просто не знает — окна отказа нет.
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_llm_usage_slices"
down_revision = "0014_drop_legacy_monitor_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_usage_slices",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column(
            "source_public_id",
            sa.String(length=36),
            nullable=False,
            server_default="",
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "chat_title", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "period",
            "source_public_id",
            "chat_id",
            name="uq_llm_usage_slices_scope",
        ),
    )
    op.create_index("ix_llm_usage_slices_user_id", "llm_usage_slices", ["user_id"])


def downgrade():
    raise RuntimeError("Forward-only migration")
