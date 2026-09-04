"""Задача 1.2 — единственная точка чтения ENV, .env больше не перезаписывается.

До задачи 1.2 POST /api/settings перезаписывал .env целиком, оставляя в нём
2–3 строки (server.py, update_env_file). На Railway ФС эфемерна — настройка
молча терялась при каждом редеплое. Контракт: конфиг читается из окружения
через pydantic-settings, и ни один модуль приложения не пишет .env.
"""

import importlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_config_cache():
    """Кэш настроек не утекает из этих тестов в остальной прогон.

    Раньше здесь применялся importlib.reload(app.config). Он и был корнем
    зла: reload оставляет ТОТ ЖЕ объект модуля, но пересоздаёт его функции,
    а модули, сделавшие `from app.config import get_settings` на импорте
    (mailer, sessions, csrf, crypto), остаются связанными со СТАРОЙ функцией
    и её кэшем. С этого момента в процессе живут два кэша настроек, и модуль
    A видит заполненный Settings, а модуль B — пустой. Диагностировалось
    мучительно: в каждой точке наблюдения значение верное, поведение — нет.
    Стоило одного падения в CI и трёх кругов поиска (4 сентября 2026).

    reload здесь не нужен: Settings() читает окружение при создании,
    кэширует только get_settings — его и чистим."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "x" * 44)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    config = importlib.import_module("app.config")
    s = config.Settings()
    assert s.database_url.startswith("postgresql+asyncpg://"), (
        f"database_url читается не из ENV: {s.database_url!r}"
    )
    assert s.app_encryption_key == "x" * 44


def test_settings_environment_and_prod_flag(monkeypatch):
    config = importlib.import_module("app.config")
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = config.Settings()
    assert s.is_production is True


def test_get_settings_is_cached(monkeypatch):
    """Одна настройка на процесс: случайные разные инстансы расползаются."""
    config = importlib.import_module("app.config")
    config.get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "x" * 44)
    assert config.get_settings() is config.get_settings()


def test_app_never_writes_env_file():
    """update_env_file не существует ни в app/, ни в server.py,
    и никакой модуль не открывает .env на запись."""
    bad_open = re.compile(r"open\(\s*ENV_FILE")
    for f in list((ROOT / "app").rglob("*.py")) + [ROOT / "server.py"]:
        src = f.read_text(encoding="utf-8")
        assert "update_env_file" not in src, f"{f.name}: update_env_file жив"
        assert not bad_open.search(src), f"{f.name}: пишет .env напрямую"


@pytest.mark.parametrize("env_file", [ROOT / ".env"])
def test_env_file_is_not_tracked_by_git(env_file):
    """Сам файл .env существует локально, но не должен попасть в коммит."""
    ignore = ROOT / ".gitignore"
    if not ignore.exists():
        pytest.skip("нет .gitignore — проверка не применима")
    patterns = {
        line.strip()
        for line in ignore.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in patterns, ".gitignore не исключает .env"


def test_scripts_do_not_duplicate_the_app_package():
    """sys.path правится только при необходимости.

    Безусловный `sys.path.insert(0, ROOT)` в скрипте создаёт вторую копию
    пакета `app` при импорте из уже настроенного окружения. У дубля свой
    `lru_cache` в `app.config`, поэтому настройки, подставленные тестом
    первому экземпляру, второй не видит. В CI это проявилось как «письмо
    сброса не дошло» в тесте страниц входа — симптом за три файла от причины.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    for script in (root / "scripts").rglob("*.py"):
        src = script.read_text(encoding="utf-8")
        for m in re.finditer(r"sys\.path\.insert\([^)]*\)", src):
            line = src.count("\n", 0, m.start()) + 1
            before = src[: m.start()].rsplit("\n", 3)[-3:]
            assert any("not in sys.path" in ln for ln in before), (
                f"{script.name}:{line} правит sys.path безусловно — "
                "возможен второй экземпляр пакета app со своим кэшем настроек"
            )


def test_app_package_is_not_imported_twice():
    """Пакет `app` существует в процессе в единственном экземпляре.

    Две копии возникают, когда в sys.path попадают относительный и
    абсолютный путь к одному корню: Python считает их разными записями.
    Тогда у каждой копии свой lru_cache настроек, и модуль A видит
    заполненный Settings, а модуль B — пустой. Диагностируется мучительно:
    в каждой точке наблюдения значение верное, а поведение — нет.

    Виновником был `prepend_sys_path = .` в alembic.ini (4 сентября 2026).
    """
    import app.config
    import app.security.sessions
    import app.services.mailer

    for module in (app.services.mailer, app.security.sessions):
        assert module.get_settings is app.config.get_settings, (
            f"{module.__name__} держит другой get_settings — значит модуль "
            "app.config был перезагружен или импортирован повторно; в процессе "
            "живут два кэша настроек, и модули видят разные Settings"
        )


def test_no_relative_paths_in_sys_path():
    """Относительная запись в sys.path — готовая вторая копия пакета."""
    import sys

    relative = [p for p in sys.path if p and not p.startswith("/") and p != ""]
    assert not relative, f"относительные записи в sys.path: {relative}"
