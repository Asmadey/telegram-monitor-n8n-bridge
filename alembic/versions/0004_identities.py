"""identities — связывание внешних идентичностей с юзерами (Фаза 6)

Revision ID: 0004_identities
Revises: 0003_chat_avatars
Create Date: 2026-09-04

Написана вручную (как 0001–0003), сверена с app.models.UserIdentity
построчно: user_id NOT NULL + FK + индекс, UNIQUE(provider, provider_uid)
— один Google-аккаунт не может быть связан с двумя юзерами.
users.password_hash был nullable с 0001 (вход только через Google).
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_identities"
down_revision = "0003_chat_avatars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identities",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_uid", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint(
            "provider", "provider_uid", name=op.f("uq_identities_provider_provider_uid")
        ),
    )
    op.create_index(op.f("ix_identities_user_id"), "identities", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_identities_user_id"), table_name="identities")
    op.drop_table("identities")
