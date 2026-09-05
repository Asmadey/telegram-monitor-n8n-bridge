"""Задача 7.1 — два сервиса Railway из одного образа (PLAN.md раздел 10).

web:    uvicorn app.main:app --host 0.0.0.0 --port $PORT   (миграции — preDeploy)
worker: python -m app.worker                               (тот же образ)

Ключевое для этого репозитория: ОБЕ команды обязаны поднимать НОВУЮ сборку
app.main / app.worker. Монолит server.py (~40 эндпоинтов без auth, К2) из
образа не запускается НИКОГДА: даже случайный деплой образа не должен
поднять незакрытую сборку. Поэтому CMD Dockerfile — часть безопасности,
а не деплойная мелочь, и тест на него — статический трипваер.

Живой деплой на Railway закрыт К2 (старые эндпоинты в server.py без auth
переносятся в модули; до переноса деплой невозможен) — здесь проверяются
КОНФИГИ в репозитории, а не живые сервисы. Поведенческий уровень (редеплой
без переавторизации в Telegram) — при первом живом деплое.

Учтено: config-as-code (railway.json) у Railway deprecated до 2026-12-01;
миграция на infrastructure-as-code — вместе с живым деплоем.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_docker_image_runs_new_assembly_never_monolith():
    """CMD образа — app.main:app; server:app в образе запрещён навсегда
    (монолит без auth, К2: деплой невозможен — и случайный тоже)."""
    src = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "app.main:app" in src, "CMD не поднимает новую сборку app.main:app"
    assert "server:app" not in src, (
        "образ запускает монолит server.py — ~40 эндпоинтов без auth (К2); "
        "образ НЕ должен уметь поднимать незакрытую сборку"
    )


def test_dockerfile_binds_railway_port():
    """Railway пробрасывает $PORT — команда слушает 0.0.0.0:$PORT
    (127.0.0.1 в контейнере = healthcheck никогда не пройдёт)."""
    src = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "0.0.0.0" in src, "uvicorn обязан слушать 0.0.0.0 в контейнере"
    assert "$PORT" in src or "${PORT" in src, "порт берётся из ENV Railway"


def test_railway_json_web_service_config():
    """web-сервис: startCommand — новая сборка, healthcheck /health,
    миграции применяются ДО старта (preDeployCommand alembic upgrade head).
    Схема меняется ТОЛЬКО миграциями (Фаза 1) — деплой не может поднять
    код без схемы."""
    cfg = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    deploy = cfg.get("deploy", {})
    assert deploy.get("healthcheckPath") == "/health", "healthcheck не на /health"
    start = deploy.get("startCommand") or ""
    assert "app.main:app" in start, f"startCommand не новая сборка: {start!r}"
    assert "0.0.0.0" in start, "startCommand обязан слушать 0.0.0.0"
    pre = deploy.get("preDeployCommand") or []
    assert any("alembic" in cmd and "upgrade" in cmd for cmd in pre), (
        f"preDeployCommand не применяет миграции: {pre!r}"
    )


def test_worker_is_module_runnable_same_image():
    """worker — тот же образ, команда `python -m app.worker` (задача 4.1):
    модуль обязан сохранять точку входа __main__ (тест subprocess-запуска
    живой — test_40; здесь трипваер, что её никто не убрал)."""
    src = (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    assert "__main__" in src, "python -m app.worker не запустится — нет точки входа"


def test_railway_json_worker_never_migrates():
    """Миграции применяет ТОЛЬКО web (preDeploy). Если воркер тоже понесёт
    preDeployCommand с alembic, два сервиса будут гонять миграции
    одновременно. Один файл конфига на оба сервиса — значит в railway.json
    миграции есть, а команду воркера переопределяет СЕРВИС (не файл):
    проверяем, что файл не пытается запускать воркер."""
    cfg = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    all_text = json.dumps(cfg)
    assert "app.worker" not in all_text, (
        "railway.json общий на оба сервиса: команда воркера здесь запустила бы "
        "воркер и ВЕБ-сервисом тоже; воркер переопределяется на уровне сервиса"
    )


# --------------------------------------------------------------------------
# Дыра, найденная при ревью 7.1: CMD и railway.json перевели на app.main,
# а Procfile остался на монолите. Тест выше был зелёным, дыра — живой:
# ровно тот случай, от которого предостерегает AGENTS.md §10.
# --------------------------------------------------------------------------


def test_procfile_never_launches_the_monolith():
    """Procfile — исполняемый артефакт запуска, а не документация.

    Его читают foreman/hivemind, Heroku и Railway с NIXPACKS-билдером. Пока
    в нём `server:app`, утверждение «монолит не запустим даже случайно»
    неверно: это и есть путь «случайно».
    """
    path = ROOT / "Procfile"
    if not path.exists():
        return
    src = path.read_text(encoding="utf-8")
    assert "server:app" not in src, (
        "Procfile поднимает монолит server.py — ~40 эндпоинтов без auth (К2)"
    )
    assert "app.main:app" in src, "Procfile не поднимает новую сборку"


def test_procfile_declares_the_worker_process():
    """Два процесса (задача 4.1): web владеет HTTP, worker — единственный
    владелец долгоживущих Telethon-клиентов. Один процесс на оба = второй
    клиент на том же auth-key = AUTH_KEY_DUPLICATED у пользователя."""
    path = ROOT / "Procfile"
    if not path.exists():
        return
    src = path.read_text(encoding="utf-8")
    assert "app.worker" in src, "Procfile не объявляет процесс воркера"


def test_monolith_source_is_not_shipped_in_the_image():
    """Глубина защиты: `docker run <image> uvicorn server:app` поднимает
    незакрытую сборку даже при правильном CMD. app/ не импортирует server,
    рантайму монолит не нужен — в образе ему делать нечего.
    """
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "server.py" in patterns, (
        "server.py попадает в образ через COPY . . — монолит остаётся "
        "запускаемым внутри контейнера в обход CMD"
    )
