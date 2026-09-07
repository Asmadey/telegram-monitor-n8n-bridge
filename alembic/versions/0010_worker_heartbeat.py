"""Отметка живости воркера: web и worker — разные процессы."""

import sqlalchemy as sa

from alembic import op

revision = "0010_worker_heartbeat"
down_revision = "0009_attempt_session"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leader", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
