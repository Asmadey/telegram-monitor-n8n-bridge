"""Receipts prevent duplicate legacy logs without skipping unrelated history."""

import sqlalchemy as sa

from alembic import op

revision = "0007_legacy_import_rows"
down_revision = "0006_worker_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_import_rows",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("source_table", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=True),
        sa.UniqueConstraint("user_id", "source_table", "source_id"),
    )
    op.create_index("ix_legacy_import_rows_user_id", "legacy_import_rows", ["user_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration; legacy import receipts must be retained"
    )
