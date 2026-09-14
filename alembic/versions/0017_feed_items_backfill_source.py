"""Карточкам ленты возвращается источник (дефект 11.9, найден владельцем).

Колонку `feed_items.monitor_id` завела ревизия 0012 и один раз заполнила у
исторических строк — по `chat_id`. Конвейер источника (11.4) `chat_id` в
карточку не кладёт (у источника каналов много), а `monitor_id` не клал никто,
и с фазы 11 до этой ревизии каждая новая строка ленты рождалась без источника:
в интерфейсе буква вместо логотипа канала, при переразборе — пустые промпты.
Писатель починен в коде; здесь возвращается источник тем строкам, что уже
лежат в базе.

Связь восстанавливается по имени: конвейер кладёт в `chat_title` карточки
заголовок ИСТОЧНИКА (`batch["chat_title"]` = `source.title`), поэтому
совпадение имени в пределах кабинета — это и есть исходная связь.

Заполняется ТОЛЬКО там, где имя в кабинете принадлежит ровно одному
источнику. Два источника с одинаковым именем, переименованный источник,
удалённый источник — строка остаётся с NULL. Ошибиться здесь хуже, чем не
угадать: неверный `monitor_id` показал бы на карточке аватарку чужого канала
и разобрал бы её чужими промптами, и отличить это от правильного поведения
уже нельзя. Пустое поле честно даёт букву, как сейчас.

Ревизия только ОБНОВЛЯЕТ данные: схему не трогает, старый код читает эту
колонку ровно так же — окна отказа нет.
"""

from alembic import op

revision = "0017_feed_items_backfill_source"
down_revision = "0016_channel_token_limit"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE feed_items SET monitor_id = (
            SELECT m.id FROM monitors m
            WHERE m.user_id = feed_items.user_id
              AND m.title = feed_items.chat_title
        )
        WHERE monitor_id IS NULL
          AND chat_id IS NULL
          AND chat_title IS NOT NULL
          AND (
            SELECT COUNT(*) FROM monitors m
            WHERE m.user_id = feed_items.user_id
              AND m.title = feed_items.chat_title
          ) = 1
        """
    )


def downgrade():
    raise RuntimeError("Forward-only migration")
