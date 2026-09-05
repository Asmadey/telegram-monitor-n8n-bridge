"""llm_usage — месячный счётчик токенов на тенанта (задача 4.5)

Revision ID: 0002_llm_usage
Revises: 0001_initial_schema
Create Date: 2026-09-02

Написана вручную (как 0001), сверена с app/models.LLMUsage построчно:
user_id NOT NULL + FK + индекс, UNIQUE(user_id, period) — по строке на
тенанта в месяц, инкременты атомарные (ON CONFLICT DO UPDATE).
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_llm_usage"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint(
            "user_id", "period", name=op.f("uq_llm_usage_user_id_period")
        ),
    )
    op.create_index(op.f("ix_llm_usage_user_id"), "llm_usage", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_usage_user_id"), table_name="llm_usage")
    op.drop_table("llm_usage")
