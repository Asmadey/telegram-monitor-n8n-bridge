"""Задача 3.1 — скоуп по пользователю на уровне слоя доступа.

Самый важный тест проекта: «один забытый where user_id == — и пользователь
A читает ленту пользователя B». Поэтому тенантность держит не память
разработчика, а слой TenantRepo, а тест перебирает тенантные модели
ИНТРОСПЕКЦИЕЙ — новая модель с user_id попадает под проверку автоматически,
её нельзя «забыть добавить в список».

404, а не 403 — на уровне данных это None, а не «найдено, но чужое»:
403 подтверждает существование объекта — это утечка информации.

Эндпоинтный свип (план задачи) гоняется по маршрутам автоматически и
сейчас скипается с явной причиной: ресурсные эндпоинты ещё не перенесены
из server.py (переносятся в Фазах 3–4, каждый перенос включает свип).
"""

import datetime
import inspect

import pytest
from fastapi import params
from sqlalchemy import BigInteger, Boolean, DateTime, Integer

from app.models import Base, Monitor
from tests.conftest import walk_routes

# пути, где тенантный фильтр неприменим или не про данные тенанта
_NON_TENANT_PREFIXES = ("/auth", "/api/admin")
_NON_TENANT_EXACT = {"/", "/static", "/health"}


def _tenant_models():
    """Все модели с колонкой user_id, кроме самой users — интроспекцией."""
    from app import models

    for name in sorted(dir(models)):
        cls = getattr(models, name)
        if (
            isinstance(cls, type)
            and issubclass(cls, Base)
            and cls is not Base
            and cls.__table__.name != "users"
            and "user_id" in cls.__table__.columns
        ):
            yield cls


def _seed_row(model, user_id: int):
    """Строка-заглушка с обязательными полями, заполненными по типу.

    Устойчиво к новым моделям: NOT NULL-колонки без дефолта получают
    значение по типу; уникальные значения различаются по user_id, чтобы
    строки A и B не конфликтовали по unique-ограничениям.
    """
    values = {}
    for col in model.__table__.columns:
        if col.primary_key:
            continue  # PK — автоинкремент/uuid-дефолт
        if col.name == "user_id":
            values[col.name] = user_id
        elif col.default is None and not col.nullable:
            if isinstance(col.type, (BigInteger, Integer)):
                values[col.name] = user_id
            elif isinstance(col.type, DateTime):
                values[col.name] = datetime.datetime.now(datetime.timezone.utc)
            elif isinstance(col.type, Boolean):
                values[col.name] = False
            else:
                values[col.name] = f"seed-{user_id}-{col.name}"
    return model(**values)


@pytest.mark.asyncio
async def test_tenant_repo_scopes_every_user_id_model(db, user_a, user_b):
    """Репозиторий юзера A не возвращает ни одной строки юзера B —
    по КАЖДОЙ модели с user_id (перечень — интроспекцией, не руками)."""
    from app.db import TenantRepo

    found = []
    for model in _tenant_models():
        db.add(_seed_row(model, user_a.id))
        db.add(_seed_row(model, user_b.id))
        found.append(model)
    await db.commit()

    # защита от вакуума: если тенантных моделей нет, тест ничего не проверяет
    assert len(found) >= 5, "тенантные модели не найдены — тест вырожден"

    repo_a = TenantRepo(db, user_a.id)
    repo_b = TenantRepo(db, user_b.id)
    for model in found:
        rows_a = (await db.scalars(repo_a.query(model))).all()
        rows_b = (await db.scalars(repo_b.query(model))).all()
        assert all(r.user_id == user_a.id for r in rows_a), (
            f"{model.__name__}: запрос A вернул чужую строку"
        )
        assert all(r.user_id == user_b.id for r in rows_b), (
            f"{model.__name__}: запрос B вернул чужую строку"
        )
        assert rows_a, f"{model.__name__}: собственные строки A не видны"


@pytest.mark.asyncio
async def test_repo_get_hides_foreign_rows_as_none(db, user_a, user_b):
    """Прямой запрос по id чужой строки — None (из него роутер сделает 404),
    а не «найдено, но чужое» (из него получается 403 — подтверждение
    существования)."""
    from app.db import TenantRepo

    monitor_b = Monitor(user_id=user_b.id, chat_target="@channel-of-b")
    db.add(monitor_b)
    await db.commit()

    repo_a = TenantRepo(db, user_a.id)
    repo_b = TenantRepo(db, user_b.id)
    assert await repo_a.get(Monitor, monitor_b.id) is None, (
        "чужой монитор виден по прямому id — это утечка существования"
    )
    assert await repo_b.get(Monitor, monitor_b.id) is not None, (
        "собственный монитор не виден владельцу"
    )


def test_get_tenant_repo_chains_through_require_user():
    """Репозиторий строится ТОЛЬКО поверх аутентифицированного юзера:
    anon не может получить TenantRepo ни с каким user_id."""
    from app.deps import get_tenant_repo, require_user

    defaults = [
        p.default for p in inspect.signature(get_tenant_repo).parameters.values()
    ]
    # fastapi.Depends — функция-фабрика, а не класс: isinstance нужно по
    # fastapi.params.Depends (ошибка теста, не реализации — TypeError вместо assert)
    assert any(
        isinstance(d, params.Depends) and d.dependency is require_user for d in defaults
    ), "get_tenant_repo не зависит от require_user — репозиторий без аутентификации"


def _tenant_resource_routes():
    """Маршруты с данными тенантов: всё защищённое, кроме auth и админки."""
    from app.main import app

    for route in walk_routes(app.routes):
        path = getattr(route, "path", None)
        if not path or path in _NON_TENANT_EXACT:
            continue
        if path.startswith(_NON_TENANT_PREFIXES):
            continue
        yield route


@pytest.mark.asyncio
async def test_user_a_cannot_see_or_touch_user_b_resources(anon_client, db):
    """Свип эндпоинт-уровня из плана: B не видит ресурсы A в списках и
    получает 404 по прямым id. Маршруты перебираются автоматически —
    перенесённый из server.py роутер попадает сюда без правки списка."""
    routes = list(_tenant_resource_routes())
    if not routes:
        pytest.skip(
            "ресурсные эндпоинты ещё в server.py (перенос — Фазы 3–4); "
            "до первого переноса свип нечего перебирать"
        )
    # первый перенесённый ресурсный роутер обязан дополнить свип:
    # сидинг данных A (маркеры), проверка списков B на отсутствие маркеров,
    # прямые id — 404. Анти-вакуум: routes не пуст (скип выше).
    raise AssertionError(
        f"свип не реализован для {len(routes)} маршрутов — задача переноса "
        "первого ресурсного роутера обязана закрыть его (PLAN 3.1)"
    )
