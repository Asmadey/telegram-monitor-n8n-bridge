"""Исход проверки канала (задача 13.6).

Канал «VASILE LUNGO | INSIDER» не разрешался ни разу с момента добавления, и
узнать об этом было неоткуда: источник просто не давал находок. Между «сегодня
пусто» и «не открывался никогда» интерфейс не различал ничего.

Три колонки на строку канала: исход, причина словами и время проверки.

NULL в `check_status` — «не проверяли», и это ТРЕТЬЕ состояние, а не синоним
здоровья. Поэтому колонка nullable без умолчания: значение по умолчанию
превратило бы непроверенный канал в проверенный, и вся задача потеряла бы
смысл.

Ревизия только ДОБАВЛЯЕТ колонки: миграции идут до старта нового кода, старый
процесс о них не знает, окна отказа нет.
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_channel_check_verdict"
down_revision = "0017_feed_items_backfill_source"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monitor_channels", sa.Column("check_status", sa.String(32), nullable=True)
    )
    op.add_column(
        "monitor_channels", sa.Column("check_detail", sa.String(512), nullable=True)
    )
    op.add_column(
        "monitor_channels",
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
