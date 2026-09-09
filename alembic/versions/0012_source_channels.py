"""Монитор становится источником: каналы уезжают в свою таблицу (11.1).

Шаг намеренно НЕ меняет поведение. Таблица создаётся и наполняется, а
воркер и интерфейс продолжают работать по старым колонкам `monitors` —
они удаляются отдельной ревизией 0013, следующим релизом.

Причина расщепления в устройстве выкладки: `preDeployCommand` поднимает
миграции ДО старта нового кода, пока старый процесс ещё обслуживает
запросы. Ревизия, которая одновременно создаёт новое и удаляет старое,
даёт минуту 500-х у живого процесса.

Перенос идемпотентен: повторный прогон не создаёт второго канала.
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_source_channels"
down_revision = "0011_heartbeat_fingerprint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monitor_channels",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "monitor_id",
            sa.BigInteger(),
            sa.ForeignKey("monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("chat_target", sa.String(255), nullable=False),
        sa.Column("chat_title", sa.String(512)),
        sa.Column("chat_username", sa.String(255)),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("limit_count", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("offset_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("extract_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_checked", sa.DateTime(timezone=True)),
        sa.Column(
            "last_sent_message_id", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("fail_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("monitor_id", "chat_target"),
        sa.UniqueConstraint("monitor_id", "chat_id"),
    )
    op.create_index(
        "ix_monitor_channels_monitor_id", "monitor_channels", ["monitor_id"]
    )
    op.create_index("ix_monitor_channels_user_id", "monitor_channels", ["user_id"])

    # --- источник обзаводится своими полями ---
    op.add_column(
        "monitors",
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "monitors",
        sa.Column("answer_prompt", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "monitors",
        sa.Column("stop_words", sa.Text(), nullable=False, server_default=""),
    )
    # собственные часы: last_checked уезжает в канал, и без этого поля
    # расписание либо не сработает никогда, либо сработает каждый тик
    op.add_column("monitors", sa.Column("last_run_at", sa.DateTime(timezone=True)))
    op.add_column(
        "monitors",
        sa.Column("running", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # у источника собственного канала нет — обязательность снимается
    op.alter_column("monitors", "chat_target", nullable=True)

    op.add_column(
        "jobs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )

    # --- история ссылается на источник; при его удалении ссылка обнуляется ---
    for table in ("sent_messages", "feed_items"):
        op.add_column(
            table,
            sa.Column(
                "monitor_id",
                sa.BigInteger(),
                sa.ForeignKey("monitors.id", ondelete="SET NULL"),
            ),
        )
        op.create_index(f"ix_{table}_monitor_id", table, ["monitor_id"])

    # --- перенос: каждый существующий канал становится источником с одним ---
    op.execute(
        """
        INSERT INTO monitor_channels (
            monitor_id, user_id, chat_target, chat_title, chat_username, chat_id,
            limit_count, offset_hours, extract_prompt, position, is_active,
            last_checked, last_sent_message_id, fail_streak
        )
        SELECT m.id, m.user_id, m.chat_target, m.chat_title, m.chat_username,
               m.chat_id, COALESCE(m.limit_count, 20), COALESCE(m.offset_hours, 24),
               COALESCE(m.prompt, ''), 0, COALESCE(m.is_active, true),
               m.last_checked, COALESCE(m.last_sent_message_id, 0), 0
        FROM monitors m
        WHERE m.chat_target IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM monitor_channels c WHERE c.monitor_id = m.id
          )
        """
    )
    # имя источника: было имя канала
    op.execute(
        "UPDATE monitors SET title = COALESCE(NULLIF(chat_title, ''), chat_target, '') "
        "WHERE title = ''"
    )
    # расписание переносит свои часы с канала на источник
    op.execute(
        "UPDATE monitors SET last_run_at = last_checked WHERE last_run_at IS NULL"
    )

    # --- история дедупликации и лента привязываются к источнику ---
    # Осиротевшие строки (канал удалён) остаются с NULL: это история, и
    # удалять её нельзя — потеря дедупликации means повторная заливка.
    op.execute(
        """
        UPDATE sent_messages SET monitor_id = (
            SELECT m.id FROM monitors m
            WHERE m.user_id = sent_messages.user_id
              AND m.chat_id = sent_messages.chat_id
            ORDER BY m.id LIMIT 1
        )
        WHERE monitor_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE feed_items SET monitor_id = (
            SELECT m.id FROM monitors m
            WHERE m.user_id = feed_items.user_id
              AND m.chat_id = feed_items.chat_id
            ORDER BY m.id LIMIT 1
        )
        WHERE monitor_id IS NULL
        """
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
