"""Задача 5.1 — разобрать index.html (PLAN.md).

3975 строк в одном файле: весь CSS, вся разметка и весь JS вместе.
Разрез: разметка остаётся в index.html, стили — в static/css/, логика —
в static/js/ по вкладкам (feed, channels, messages, integration, logs)
плюс общие api.js, render.js, auth.js и вход main.js. Сборку НЕ заводим —
ES-модули (`<script type="module">`).

Структурные тесты ниже — это обратная связь для рефакторинга БЕЗ
браузерных тестов: инлайн-обработчики держатся на window-глобалах,
импорты обязаны резолвиться, каждый файл обязан парситься (node --check
на .mjs-копии). XSS-сканер задачи 0.4 ходит по static/**/*.{html,js}
рекурсивно — покрытие само следует за кодом.
"""

import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INDEX = STATIC / "index.html"
JS_DIR = STATIC / "js"
CSS_DIR = STATIC / "css"

REQUIRED_MODULES = (
    "api.js",
    "render.js",
    "auth.js",
    "feed.js",
    "channels.js",
    "messages.js",
    "integration.js",
    "logs.js",
    "main.js",
)


def _index_source() -> str:
    assert INDEX.exists(), "static/index.html отсутствует"
    return INDEX.read_text(encoding="utf-8")


def _js_sources() -> dict[str, str]:
    if not JS_DIR.exists():
        return {}
    return {f.name: f.read_text(encoding="utf-8") for f in sorted(JS_DIR.glob("*.js"))}


def test_index_has_no_inline_css_or_js():
    """Стили и логика НЕ живут в разметке: ни одного <style>-блока,
    каждый <script> — внешний (CDN gsap + ES-модуль main.js)."""
    source = _index_source()
    assert "<style" not in source, "CSS не вынесен из index.html"
    for tag in re.findall(r"<script\b[^>]*>", source):
        assert "src=" in tag, f"инлайн-скрипт остался в разметке: {tag}"


def test_css_extracted():
    """Стили вынесены в static/css/ и подключены ссылкой; содержимое
    сохранилось (корневые переменные и селектор вкладок — из оригинала)."""
    files = sorted(CSS_DIR.glob("*.css")) if CSS_DIR.exists() else []
    assert files, "static/css/ пуст — стили не вынесены"
    css = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert ":root" in css, "корневые переменные темы потерялись"
    assert ".tab-btn" in css, "стили вкладок потерялись"
    source = _index_source()
    assert '<link rel="stylesheet"' in source, "css не подключён к разметке"


def test_js_split_by_tabs():
    """Логика разрезана: общие api/render/auth, по модулю на вкладку,
    вход main.js. Никакой модуль не пустой."""
    sources = _js_sources()
    for name in REQUIRED_MODULES:
        assert name in sources, f"static/js/{name} отсутствует"
        assert sources[name].strip(), f"static/js/{name} пуст"
    source = _index_source()
    assert 'src="/static/js/main.js"' in source, "main.js не подключён модулем"


def test_index_is_markup_only():
    """index.html — только разметка: ~2000 строк стилей и JS ушли,
    остаётся меньше 1500."""
    lines = _index_source().splitlines()
    assert len(lines) < 1500, (
        f"index.html всё ещё {len(lines)} строк — разметка не разрезана"
    )


def test_module_imports_resolve():
    r"""Каждый относительный импорт резолвится в существующий файл, и
    каждое импортируемое имя ЭКСПОРТИРУЕТСЯ целью — иначе модуль падает
    в рантайме браузера, а тестов браузера у нас нет.

    Дефекты первой версии (пойманы задачей 5.3, когда перезапись auth.js
    прошла незамеченной): (1) regex импортов требовал двойные кавычки,
    а модули пишут одинарные — импорты НЕ находились вовсе, тест был
    вакуумным; (2) экспорты собирались из исходника ИМПОРТЁРА, а не
    цели импорта. Теперь карта экспортов строится для всех файлов, а
    проверка смотрит экспорты именно цели."""
    sources = _js_sources()
    assert sources, "static/js/ пуст"
    export_re = re.compile(
        r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)"
        r"|export\s*\{([^}]*)\}"
    )

    def _exports_of(src: str) -> set[str]:
        out: set[str] = set()
        for m in export_re.finditer(src):
            out.update(filter(None, m.groups()[0:1]))
            if m.group(2):
                out.update(
                    part.strip().split(" as ")[-1].strip()
                    for part in m.group(2).split(",")
                    if part.strip()
                )
        return out

    exports_map = {name: _exports_of(src) for name, src in sources.items()}
    import_re = re.compile(
        r"import\s+(?:\{([^}]*)\}|(\w+))\s*from\s*['\"](\./[^'\"]+)['\"]"
    )
    violations = []
    for name, src in sources.items():
        for m in import_re.finditer(src):
            target_name = Path(m.group(3)).name
            target = JS_DIR / target_name
            if not target.exists():
                violations.append(f"{name}: нет файла {m.group(3)}")
                continue
            wanted = set()
            if m.group(1):
                wanted = {
                    part.strip().split(" as ")[0].strip()
                    for part in m.group(1).split(",")
                    if part.strip()
                }
            if m.group(2):
                wanted = {m.group(2)}
            # экспорты смотрим У ЦЕЛИ импорта, не у импортёра
            target_exports = exports_map.get(
                target_name,
                _exports_of(target.read_text(encoding="utf-8")),
            )
            missing = wanted - target_exports
            if missing:
                violations.append(
                    f"{name}: {m.group(3)} не экспортирует {sorted(missing)}"
                )
    assert violations == [], f"битые импорты: {violations}"


def test_inline_handlers_have_window_globals():
    """Инлайн-onclick остаются в разметке/шаблонах (минимальный порт), а
    ES-модули — file-scoped: каждая функция из onclick ОБЯЗАНА быть
    выставлена на window в каком-то модуле, иначе кнопка молча мертва."""
    sources = _js_sources()
    all_js = "\n".join(sources.values())
    called = set(re.findall(r'onclick="(\w+)\(', _index_source() + all_js))
    called.update(re.findall(r"onchange=\"(\w+)\(", _index_source() + all_js))
    assert called, "не найдено ни одного inline-обработчика — разметка не та"
    exposed = set(re.findall(r"window\.(\w+)\s*=", all_js))
    dead = called - exposed
    assert not dead, (
        f"inline-обработчики без window-глобала (кнопки мертвы): {sorted(dead)}"
    )


def test_no_build_system_introduced():
    """Сборку НЕ заводим (план 5.1): ES-модули без единого пакетного
    менеджера — package.json/node_modules в static/ запрещены."""
    assert not (STATIC / "package.json").exists()
    assert not (STATIC / "node_modules").exists()


def test_js_modules_parse():
    """Синтаксис каждого модуля проверяет node --check (.mjs-копия —
    node парсит ES-модули только по расширению/тайпу). Без браузерных
    тестов это единственный автоматический барьер опечаток."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — синтаксис модулей не проверить")
    sources = _js_sources()
    assert sources
    for name, src in sources.items():
        tmp = STATIC / f".{name}.check.mjs"
        try:
            tmp.write_text(src, encoding="utf-8")
            subprocess.run(
                [node, "--check", str(tmp)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            pytest.fail(f"{name} не парсится:\n{e.stderr.decode(errors='replace')}")
        finally:
            tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_monolith_serves_split_assets():
    """server.py — рантайм до закрытия К2 (перенос старых эндпоинтов), и
    разрез 5.1 обязан работать НА НЁМ: index.html ссылается на
    /static/css/main.css и /static/js/main.js, монолит обязан их раздавать.
    Иначе UI оператора после разреза мёртв (голая разметка без стилей
    и логики). ASGITransport не запускает lifespan — фоновый планировщик
    и Telethon-клиент не стартуют.

    Поведенческий уровень: без окружения (нет .env с ключами Telegram)
    импорт server.py падает — честный skip, не молчаливая зелень.
    """
    try:
        import server  # noqa: PLC0415 — импорт в тесте, лениво и осознанно
    except Exception as e:  # pragma: no cover — зависит от окружения
        pytest.skip(f"server.py не импортируется без окружения: {e}")

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for url, ctype in (
            ("/static/css/main.css", "text/css"),
            ("/static/js/main.js", "javascript"),
            ("/static/js/api.js", "javascript"),
        ):
            resp = await client.get(url)
            assert resp.status_code == 200, (
                f"{url} не раздаётся монолитом — разрезанный UI мёртв"
            )
            assert ctype in resp.headers.get("content-type", ""), (
                f"{url}: content-type {resp.headers.get('content-type')!r}"
            )
