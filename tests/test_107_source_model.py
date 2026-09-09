"""Источник и его каналы: модель и перенос (задача 11.1).

Фаза 11 разделяет то, что монолит склеил в одну строку: «где искать»
(канал), «что искать» (промпт извлечения, свой у каждого канала) и «как
оформить ответ» (промпт источника). `monitors` становится источником,
каналы уезжают в отдельную таблицу.

Шаг намеренно не меняет поведение: таблица создаётся и наполняется, но
воркер продолжает работать по-старому. Так выкладка расщепляется на
безопасные части — `preDeployCommand` поднимает миграции ДО старта нового
кода, и ревизия, которая одновременно создаёт и удаляет колонки, даёт
минуту 500-х у ещё живого старого процесса.

Два правила закреплены здесь, потому что нарушить их можно только один
раз и необратимо:

1. **Удаление источника не удаляет ни ленту, ни историю дедупликации.**
   Лента — история и должна пережить источник. Дедупликация тем более:
   каскад означал бы, что пересозданный источник зальёт в n8n все старые
   посты заново — ровно тот риск на 192 дубля, которым открывался план.
2. **Каналы источника, наоборот, уезжают вместе с ним** — сами по себе они
   не значат ничего.
"""

import pytest


def _table(name: str):
    from app.models import Base

    return Base.metadata.tables[name]


def test_source_channels_table_exists_with_tenant_column():
    """Тенантное правило (AGENTS §5) — на новой таблице тоже.

    Свип `test_12_models.py` держит его автоматически, здесь — явная
    проверка, чтобы падение было читаемым.
    """
    channels = _table("monitor_channels")
    assert "user_id" in channels.c and not channels.c.user_id.nullable
    assert "monitor_id" in channels.c and not channels.c.monitor_id.nullable


def test_channel_carries_what_used_to_live_on_the_monitor():
    channels = _table("monitor_channels")
    for name in (
        "chat_target",
        "chat_title",
        "chat_username",
        "chat_id",
        "limit_count",
        "offset_hours",
        "extract_prompt",
        "position",
        "is_active",
        "last_checked",
        "last_sent_message_id",
        "fail_streak",
    ):
        assert name in channels.c, f"у канала нет поля {name}"


def test_source_gets_its_own_fields():
    monitors = _table("monitors")
    for name in ("title", "answer_prompt", "stop_words", "last_run_at", "running"):
        assert name in monitors.c, f"у источника нет поля {name}"


def test_schedule_has_its_own_clock():
    """`_is_due` считал по `last_checked`, а оно уезжает в канал.

    Без собственных часов источник либо не запустится никогда, либо
    станет запускаться каждый тик — дефект найден аудитом плана, а не
    прогоном.
    """
    monitors = _table("monitors")
    assert "last_run_at" in monitors.c, "источнику нечем мерить свой интервал"


def test_jobs_count_attempts():
    """Потолок попыток некуда было записывать: у задачи нет счётчика."""
    assert "attempts" in _table("jobs").c


@pytest.mark.parametrize("table", ["sent_messages", "feed_items"])
def test_history_survives_the_source(table):
    """Ссылка на источник обнуляется, а строка остаётся.

    Каскад здесь означал бы потерю дедупликации: пересозданный источник
    залил бы в n8n все старые посты заново.
    """
    column = _table(table).c.monitor_id
    assert column.nullable, f"{table}.monitor_id обязателен — источник не удалить"
    actions = {fk.ondelete for fk in column.foreign_keys}
    assert actions == {"SET NULL"}, (
        f"{table}.monitor_id при удалении источника делает {actions}, "
        "а должен обнуляться: история переживает источник"
    )


def test_channels_die_with_their_source():
    """Канал без источника не значит ничего — он уезжает вместе с ним."""
    column = _table("monitor_channels").c.monitor_id
    actions = {fk.ondelete for fk in column.foreign_keys}
    assert actions == {"CASCADE"}, f"канал при удалении источника делает {actions}"


@pytest.mark.asyncio
async def test_same_channel_cannot_be_added_twice_to_one_source(db, user):
    """Дважды один канал в одном источнике — двойной опрос и двойной счёт."""
    from sqlalchemy.exc import IntegrityError

    from app.models import Monitor, MonitorChannel

    source = Monitor(user_id=user.id, public_id="src-1", title="Вакансии")
    db.add(source)
    await db.commit()

    db.add(
        MonitorChannel(
            monitor_id=source.id,
            user_id=user.id,
            chat_target="@jobs",
            chat_id=-1001,
            extract_prompt="искать продажи",
        )
    )
    await db.commit()

    db.add(
        MonitorChannel(
            monitor_id=source.id,
            user_id=user.id,
            chat_target="@jobs",
            chat_id=-1001,
            extract_prompt="искать продажи",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_same_channel_in_two_sources_is_allowed(db, user):
    """Решение владельца: канал может входить в несколько источников."""
    from app.models import Monitor, MonitorChannel

    for index in (1, 2):
        source = Monitor(user_id=user.id, public_id=f"src-{index}", title=f"И{index}")
        db.add(source)
        await db.commit()
        db.add(
            MonitorChannel(
                monitor_id=source.id,
                user_id=user.id,
                chat_target="@jobs",
                chat_id=-1001,
                extract_prompt=f"критерии {index}",
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_migration_turns_an_old_monitor_into_a_source(alembic_target_db):
    """Поведенческий уровень: живой Postgres, реальная ревизия.

    Канал, заведённый до Фазы 11, обязан стать источником с одним каналом
    без потери промпта, лимита и интервала. Повторный прогон миграций
    ничего не меняет.
    """
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0011_heartbeat_fingerprint")

    engine = create_async_engine(alembic_target_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (email, is_admin, timezone) "
                    "VALUES ('legacy@example.com', false, 'UTC')"
                )
            )
            user_id = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = 'legacy@example.com'")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO monitors (public_id, user_id, chat_target, "
                    "chat_title, chat_id, interval_minutes, limit_count, "
                    "offset_hours, is_active, last_sent_message_id, prompt) "
                    "VALUES ('legacy-1', :u, '@finder', 'Finder.work', -100777, "
                    "360, 50, 24, true, 0, 'искать вакансии продаж')"
                ),
                {"u": user_id},
            )

        command.upgrade(cfg, "head")

        async with engine.connect() as conn:
            channel = (
                await conn.execute(
                    text(
                        "SELECT chat_target, limit_count, extract_prompt "
                        "FROM monitor_channels WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).all()
            assert len(channel) == 1, f"канал не переехал в источник: {channel}"
            assert channel[0].chat_target == "@finder"
            assert channel[0].limit_count == 50, "лимит сообщений потерян"
            assert channel[0].extract_prompt == "искать вакансии продаж", (
                "промпт канала потерян при переносе"
            )

            source = (
                await conn.execute(
                    text(
                        "SELECT title, interval_minutes FROM monitors "
                        "WHERE public_id = 'legacy-1'"
                    )
                )
            ).one()
            assert source.title == "Finder.work", "источник остался без имени"
            assert source.interval_minutes == 360, "интервал потерян"

        # идемпотентность: повторный прогон не создаёт второй канал
        command.upgrade(cfg, "head")
        async with engine.connect() as conn:
            total = (
                await conn.execute(
                    text("SELECT count(*) FROM monitor_channels WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
            assert total == 1, f"повторный прогон продублировал канал: {total}"
    finally:
        await engine.dispose()
