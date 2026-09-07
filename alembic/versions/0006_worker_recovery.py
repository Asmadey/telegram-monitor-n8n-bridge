"""Persist worker retry deadlines and distinguish reservations from processed posts."""

import sqlalchemy as sa

from alembic import op

revision = "0006_worker_recovery"
down_revision = "0005_cleanup_and_public_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "telegram_accounts",
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "feed_items", sa.Column("analysis_progress_json", sa.Text(), nullable=True)
    )
    op.add_column("feed_items", sa.Column("bot_status", sa.String(32), nullable=True))
    op.add_column(
        "feed_items", sa.Column("webhook_status", sa.String(32), nullable=True)
    )
    op.add_column(
        "jobs", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "sent_messages",
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
