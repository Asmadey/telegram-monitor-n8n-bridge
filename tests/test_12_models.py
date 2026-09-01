"""Задача 1.3 — многотенантность на уровне схемы: user_id везде.

Сегодня все таблицы глобальные: любой, кто попал на сервер, видит чужие
каналы и чужую ленту. Контракт: у каждой тезисной таблицы есть NOT NULL
user_id с индексом — без него фильтр по тенанту вырождается в full scan
и его «забывают» добавить в очередной запрос.
"""
from app.models import Base

# Таблицы с данными конкретного пользователя (PLAN.md, задача 1.3).
TENANT_TABLES = [
    "monitors",
    "sent_messages",
    "feed_items",
    "logs",
    "integrations",
    "telegram_accounts",
    "jobs",
]

# users — сам пользователь; sessions и tg_auth_attempts ссылаются на user_id
# внешним ключом, но не являются «данными тенанта» в этом смысле.


def test_tenant_tables_exist():
    for name in TENANT_TABLES:
        assert name in Base.metadata.tables, f"нет таблицы {name}"


def test_every_tenant_table_has_indexed_not_null_user_id():
    for name in TENANT_TABLES:
        t = Base.metadata.tables[name]
        assert "user_id" in t.c, f"{name}: нет user_id"
        assert not t.c.user_id.nullable, f"{name}: user_id допускает NULL"
        assert any(
            "user_id" in i.columns for i in t.indexes
        ), f"{name}: user_id без индекса"


def test_user_id_is_foreign_key_to_users():
    """user_id — настоящий FK на users.id, а не просто число."""
    users = Base.metadata.tables["users"]
    for name in TENANT_TABLES:
        t = Base.metadata.tables[name]
        fks = list(t.c.user_id.foreign_keys)
        assert fks, f"{name}: user_id без внешнего ключа"
        assert fks[0].column.table is users, f"{name}: user_id ссылается не на users"


def test_sent_messages_unique_per_tenant():
    """Дедуп теперь в разрезе тенанта: UNIQUE(user_id, chat_id, message_id)."""
    t = Base.metadata.tables["sent_messages"]
    combos = {
        tuple(sorted(c.name for c in u.columns)) for u in t.constraints
        if getattr(u, "columns", None) and u.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_id", "message_id", "user_id") in combos, (
        f"нет UNIQUE(user_id, chat_id, message_id), найдено: {combos}"
    )


def test_monitors_use_bigint_pk_and_public_id():
    """id — BIGINT, публичный идентификатор — отдельная колонка public_id."""
    t = Base.metadata.tables["monitors"]
    assert str(t.c.id.type) == "BIGINT", f"monitors.id: {t.c.id.type}"
    assert "public_id" in t.c, "нет public_id"


def test_integrations_have_no_singleton_check():
    """integrations — по строке на пользователя, CHECK (id = 1) больше нет."""
    t = Base.metadata.tables["integrations"]
    for c in t.constraints:
        src = getattr(c, "sqltext", None)
        assert src is None or "id = 1" not in str(src), "остался синглтон-CHECK"
    assert not t.c.user_id.nullable, "integrations.user_id должен быть NOT NULL"