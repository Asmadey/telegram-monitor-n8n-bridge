"""Исход последнего прогона у источника (задача 13.32).

Колонка только ДОБАВЛЯЕТСЯ: миграции идут до старта нового кода, старый
процесс о ней не знает (факт 2 CLAUDE.md). Пусто — прогона ещё не было или
он прошёл до этой ревизии.

Исход прогона по расписанию раньше оставался только в журнале: источник,
который неделю отвечает «новых постов нет», на карточке выглядел так же,
как работающий.
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_source_last_outcome"
down_revision = "0020_source_html_markup"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("monitors", sa.Column("last_outcome", sa.String(32), nullable=True))


def downgrade():
    raise RuntimeError(
        "миграции только вперёд: откат уронил бы работающий код, "
        "который уже читает эту колонку"
    )
