"""Ключ дедупликации считается по источнику (задача 11.2).

Прежний ключ `(user_id, chat_id, message_id)` был верен, пока монитор
равнялся каналу. С приходом источников один канал может входить в
несколько источников с разными критериями — и при прежнем ключе пост,
увиденный первым источником, для второго переставал существовать.

Индекс частичный: строки без источника (история удалённого — задача 11.1)
в ключ не входят и дедупликации не мешают.

**Порядок важен.** Сначала создаётся новый ключ, потом снимается старый:
наоборот — значит окно, в котором уникальности нет вовсе, и конкурентная
вставка успеет положить дубль.

**Известное окно выкладки.** Миграции поднимаются ДО старта нового кода,
и между снятием старого ограничения и запуском нового процесса старый
код ещё жив: его `ON CONFLICT (user_id, chat_id, message_id)` цели не
найдёт и опрос упадёт. Отказ восстановимый — задача вернётся в очередь,
запись останется в журнале — и длится он около минуты. Разнести это на
два релиза нельзя: пока старое ограничение живо, второй источник получает
не «пропустить», а нарушение чужого ограничения.
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_source_dedup_key"
down_revision = "0012_source_channels"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "uq_sent_messages_source_dedup",
        "sent_messages",
        ["monitor_id", "chat_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("monitor_id IS NOT NULL"),
    )
    # Имя ограничения генерировала СУБД, поэтому снимаем его по составу
    # колонок, а не по угаданному имени: промах означал бы, что старый ключ
    # остался и второй источник получает нарушение вместо «пропустить».
    op.execute(
        """
        DO $$
        DECLARE target text;
        BEGIN
            SELECT c.conname INTO target
            FROM pg_constraint c
            WHERE c.conrelid = 'sent_messages'::regclass
              AND c.contype = 'u'
              AND (
                  SELECT array_agg(a.attname::text ORDER BY a.attname)
                  FROM unnest(c.conkey) k
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = k
              ) = ARRAY['chat_id', 'message_id', 'user_id'];
            IF target IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE sent_messages DROP CONSTRAINT %I', target
                );
            END IF;
        END $$;
        """
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
