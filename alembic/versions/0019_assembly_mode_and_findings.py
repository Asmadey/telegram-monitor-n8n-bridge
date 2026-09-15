"""Режим сборки источника и находки как данные (задача 13.9).

Две колонки, обе только ДОБАВЛЯЮТСЯ: миграции идут до старта нового кода,
старый процесс о них не знает, окна отказа нет (факт 2 CLAUDE.md).

`monitors.assembly_mode` — 'prompt' или 'template'. Умолчание 'prompt': у всех
существующих источников промпт оформления уже написан, и смена поведения на
выкладке была бы сюрпризом, а не улучшением. Переключает владелец.

`feed_items.findings_json` — находки СТРУКТУРОЙ, а не отрисовкой. До сих пор в
ленте лежал готовый текст, и поэтому кабинет не мог нарисовать карточки, n8n
получал строку вместо массива, а переотрисовка требовала модели.
"""

import sqlalchemy as sa

from alembic import op

revision = "0019_assembly_mode_and_findings"
down_revision = "0018_channel_check_verdict"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monitors",
        sa.Column(
            "assembly_mode",
            sa.String(16),
            nullable=False,
            server_default="prompt",
        ),
    )
    op.add_column("feed_items", sa.Column("findings_json", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError(
        "миграции только вперёд: откат уронил бы работающий код, "
        "который уже читает эти колонки"
    )
