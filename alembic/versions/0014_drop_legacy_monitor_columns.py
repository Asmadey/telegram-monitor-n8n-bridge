"""Снятие колонок канала с источника (задача 11.8).

Ревизия `0012` скопировала их в `monitor_channels` и оставила на месте: в
той выкладке по ним ещё работали воркер и интерфейс. Теперь читает и пишет
только новая таблица, и колонки-двойники пора убрать — оставленные, они
расходятся с настоящими данными молча, и следующий агент неизбежно
прочитает не то.

Второй шаг двухшаговой выкладки: `preDeployCommand` поднимает миграции ДО
старта нового кода, поэтому удалять то, чем прежний код ещё пользуется,
нельзя было в одной ревизии с созданием.
"""

from alembic import op

revision = "0014_drop_legacy_monitor_columns"
down_revision = "0013_source_dedup_key"
branch_labels = None
depends_on = None

LEGACY = (
    "chat_target",
    "chat_title",
    "chat_username",
    "chat_id",
    "limit_count",
    "offset_hours",
    "last_checked",
    "last_sent_message_id",
    "prompt",
)


def upgrade():
    # Страховка на случай, если перенос 0012 где-то не отработал: канал без
    # строки в monitor_channels потерялся бы безвозвратно.
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
    # И часы источника — если 0012 их не проставил
    op.execute(
        "UPDATE monitors SET last_run_at = last_checked "
        "WHERE last_run_at IS NULL AND last_checked IS NOT NULL"
    )
    for column in LEGACY:
        op.drop_column("monitors", column)


def downgrade():
    raise RuntimeError("Forward-only migration")
