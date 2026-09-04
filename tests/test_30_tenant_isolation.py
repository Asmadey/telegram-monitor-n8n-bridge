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
import json
import re

import pytest
from fastapi import params
from sqlalchemy import BigInteger, Boolean, DateTime, Integer

from app.models import Base, Monitor
from tests.conftest import walk_routes

# пути, где тенантный фильтр неприменим или не про данные тенанта.
# /api/telegram — поток ВХОДА (send-code/sign-in): изоляция попыток по
# user_id проверена отдельно в test_32, ресурсных id-путей там нет.
_NON_TENANT_PREFIXES = ("/auth", "/api/admin", "/api/telegram")
# Экраны входа (5.3) — статическая разметка форм для анонима,
# данных тенантов не отдают.
_NON_TENANT_EXACT = {"/", "/static", "/health", "/login", "/signup", "/password-reset"}


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
    # Публичные маршруты берём из белого списка test_22 — один источник
    # истины о том, что открыто анониму. Маршрут, открытый по замыслу, не
    # может быть ресурсом тенанта: оболочка SPA отдаёт HTML без данных, и
    # разбирать её как JSON нечего.
    from test_22_auth_required import _is_public

    from app.main import app

    for route in walk_routes(app.routes):
        path = getattr(route, "path", None)
        if not path or path in _NON_TENANT_EXACT or _is_public(path):
            continue
        if path.startswith(_NON_TENANT_PREFIXES):
            continue
        yield route


@pytest.mark.asyncio
async def test_user_a_cannot_see_or_touch_user_b_resources(
    anon_client, db, user_a, user_b
):
    """Свип эндпоинт-уровня из плана: B не видит ресурсы A в списках и
    получает 404 по прямым id. Маршруты перебираются автоматически —
    следующий перенесённый из server.py роутер попадает сюда без правки
    списков; неизвестный path-параметр или метод падает ГРОМКО: свип
    обязан расти вместе с роутерами, молча пропускать нельзя.

    Закрыт задачей 5.4 — первым ресурсным роутером (лента + аватарки):
    сидинг данных A с маркерами, списки B — без маркеров (и со СВОИМИ
    данными — позитивный контроль, иначе «пусто» выглядело бы зелёным),
    прямые id/параметры — данные A → 404."""
    routes = list(_tenant_resource_routes())
    if not routes:
        pytest.skip(
            "ресурсные эндпоинты ещё в server.py (перенос — Фазы 3–4); "
            "до первого переноса свип нечего перебирать"
        )

    from app.models import ChatAvatar, FeedItem, LogEntry, SentMessage
    from tests.conftest import act_as

    marker = "tenant-a-secret-marker"
    chat_a, chat_b = 424242501, 424242502
    monitor_a = Monitor(
        user_id=user_a.id,
        public_id="a-monitor",
        chat_target="@a-channel",
        chat_title=marker,
        chat_id=chat_a,
    )
    db.add(monitor_a)
    db.add(ChatAvatar(chat_id=chat_a, image_bytes=b"\xff\xd8avatar-of-a"))
    feed_a = FeedItem(
        user_id=user_a.id,
        job_id="job-of-a",
        chat_id=chat_a,
        chat_title=marker,
        messages_count=1,
        ai_analysis=marker,
        raw_messages_json=json.dumps([{"id": 1, "text": marker}]),
    )
    db.add(feed_a)
    # посты и журнал A: те же списки обязаны не отдавать их B
    db.add(
        SentMessage(
            user_id=user_a.id,
            chat_id=chat_a,
            message_id=1,
            text=marker,
            reactions_json="[]",
        )
    )
    db.add(LogEntry(user_id=user_a.id, event_type="X", status="INFO", details=marker))
    # собственные данные B: списки/эндпоинты обязаны работать и отдавать своё
    # заголовок несёт тот же маркер «сводка B»: позитивный контроль свипа
    # ищет его в ответе КАЖДОГО списка, а у каналов видимое поле — название
    db.add(
        Monitor(
            user_id=user_b.id,
            chat_target="@b-channel",
            chat_title="сводка B",
            chat_id=chat_b,
        )
    )
    db.add(ChatAvatar(chat_id=chat_b, image_bytes=b"\xff\xd8avatar-of-b"))
    db.add(
        FeedItem(
            user_id=user_b.id,
            job_id="job-of-b",
            chat_id=chat_b,
            chat_title="канал B",
            messages_count=0,
            ai_analysis="сводка B",
            raw_messages_json="[]",
        )
    )
    db.add(
        SentMessage(
            user_id=user_b.id,
            chat_id=chat_b,
            message_id=2,
            text="сводка B",
            reactions_json="[]",
        )
    )
    db.add(
        LogEntry(user_id=user_b.id, event_type="X", status="INFO", details="сводка B")
    )
    await db.commit()

    # секреты интеграций A: конфиг-эндпоинты не отдают их даже владельцу
    # (только маска), а чужому — тем более. Маркер внутри значения делает
    # проверку утечки настоящей и для них.
    from app.services.integrations import save_integration_secrets

    await save_integration_secrets(
        db, user_a.id, webhook_url=f"https://example.com/hook/{marker}"
    )
    await db.commit()

    await act_as(anon_client, db, user_b)

    # ЧУЖИЕ (A) ресурсы в path-параметрах: каждый маршрут свипа обязан
    # знать, чем их подставлять — новый параметр = громкий провал здесь
    params = {"id": feed_a.id, "chat_id": chat_a, "public_id": "a-monitor"}
    exercised = set()
    for route in routes:
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            assert method in ("GET", "HEAD", "OPTIONS", "POST", "PATCH", "DELETE"), (
                f"свип не умеет {method} {route.path} — дополни свип (PLAN 3.1)"
            )
            if method in ("HEAD", "OPTIONS"):
                continue
            if method != "GET" and "{" not in route.path:
                # Коллекционный не-GET (создание, пакетное действие) — чужой
                # ресурс подставить не во что: изоляции здесь взяться неоткуда,
                # проверяется тестом самого роутера. Пропуск осознанный, и
                # он не молчаливый: маршрут остаётся в exercised ниже.
                exercised.add(f"{method} {route.path}")
                continue

            def _sub(path: str, path_route=route.path) -> str:
                def repl(m: re.Match) -> str:
                    name = m.group(1)
                    if name not in params:
                        pytest.fail(
                            f"свип не знает параметр {name} маршрута "
                            f"{path_route} — дополни params"
                        )
                    return str(params[name])

                return re.sub(r"\{(\w+)\}", repl, path)

            url = _sub(route.path)
            if method == "GET":
                resp = await anon_client.get(url)
            else:
                # изменяющий метод по ЧУЖОМУ id: тело пустое — до валидации
                # дело дойти не должно, ресурс не найден раньше
                resp = await anon_client.request(method, url, json={})
            exercised.add(f"{method} {route.path}")
            if method != "GET":
                assert resp.status_code == 404, (
                    f"{method} {url} для юзера B → {resp.status_code}, должен 404: "
                    "изменяющий метод по чужому ресурсу"
                )
            elif "{" in route.path:
                # прямой доступ к ЧУЖИМУ ресурсу: 404, не 403 и не 200
                assert resp.status_code == 404, (
                    f"{url} для юзера B → {resp.status_code}, должен 404 "
                    "(403 подтверждает существование)"
                )
            else:
                # списочный маршрут: 200, НИ ОДНОГО маркера A, но своё — видно
                assert resp.status_code == 200, f"{url} → {resp.status_code}"
                assert marker not in resp.text, f"{url} утёк маркер тенанта A"
                # Позитивный контроль — только для СПИСКОВ. Конфигурационные
                # эндпоинты (/api/webhook, /api/openrouter, ...) отдают одну
                # строку настроек и по замыслу не показывают ни секрета, ни
                # чужих данных: маркеру там взяться неоткуда. Проверка утечки
                # выше применяется к ним наравне со списками.
                body = resp.json()
                has_list = isinstance(body, dict) and any(
                    isinstance(v, list) for v in body.values()
                )
                if has_list:
                    assert "сводка B" in resp.text, (
                        f"{url}: собственных данных B не видно — свип не видит, "
                        "что списки вообще работают"
                    )

    # анти-вакуум: каждый маршрут свипа реально проверен — по КАЖДОМУ методу,
    # а не только по GET: изменяющие методы и есть самый опасный путь утечки
    expected = {
        f"{m} {r.path}"
        for r in routes
        for m in (getattr(r, "methods", None) or {"GET"})
        if m not in ("HEAD", "OPTIONS")
    }
    assert exercised == expected, (
        f"свип покрыл не все маршруты: не проверено {sorted(expected - exercised)}"
    )

    # позитивный контроль аватарки: владельцу монитора отдаётся своя
    own = await anon_client.get(f"/api/avatars/{chat_b}")
    assert own.status_code == 200 and own.content == b"\xff\xd8avatar-of-b", (
        f"собственная аватарка юзера B → {own.status_code}"
    )
