"""Сессия, запросившая код подтверждения, хранится с попыткой входа.

Telegram привязывает попытку к auth-key той сессии, что вызвала
send_code_request; подтверждать код должна она же.
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_attempt_session"
down_revision = "0008_telegram_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tg_auth_attempts",
        sa.Column("session_string_encrypted", sa.Text(), nullable=True),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
