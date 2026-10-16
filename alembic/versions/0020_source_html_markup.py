"""Флажок «Применить HTML-разметку» у источника (задача 13.27).

Колонка только ДОБАВЛЯЕТСЯ: миграции идут до старта нового кода, старый
процесс о ней не знает и вставляет строки без неё, а умолчание на стороне
базы заполняет её за него (факт 2 CLAUDE.md).

Умолчание — выключен: включённый без спроса флажок поменял бы вывод всем
источникам, чей промпт уже требует свой формат. Включает владелец.
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_source_html_markup"
down_revision = "0019_assembly_mode_and_findings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monitors",
        sa.Column(
            "html_markup", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    raise RuntimeError(
        "миграции только вперёд: откат уронил бы работающий код, "
        "который уже читает эту колонку"
    )
