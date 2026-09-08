"""Отпечаток ключа шифрования рядом с ударом сердца воркера.

Сам ключ никуда не пишется: только 8 знаков SHA-256, по которым видно
расхождение между процессами.
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_heartbeat_fingerprint"
down_revision = "0010_worker_heartbeat"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "worker_heartbeats",
        sa.Column("key_fingerprint", sa.String(16), nullable=True),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
