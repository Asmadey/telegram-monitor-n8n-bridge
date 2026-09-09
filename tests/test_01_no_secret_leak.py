"""Задача 0.3 — HTTP-эндпоинты не возвращают секреты в открытом виде.

GET /api/openrouter и GET /api/telegram-forward отдавали сырой api_key и
bot_token рядом с маской. Проверка статическая: разбираем AST и смотрим, что
именно возвращают функции-эндпоинты.

**Найдено 9 сентября: проверка выродилась и об этом никто не узнал.**
Она искала декоратор `@app.<метод>`. Пока весь бэкенд был монолитом
`server.py`, это и означало «все эндпоинты». Задача 5.1 разложила роуты по
`APIRouter`, декоратор стал `@router.<метод>` — и свип перестал видеть все
сорок эндпоинтов приложения, продолжая честно зеленеть на четырёх
статических страницах `main.py`.

Это ровно тот же класс отказа, что и вчерашний дефект секретных полей:
проверка, которая ничего не проверяет, хуже отсутствующей — отсутствующую
видно. Поэтому здесь добавлен счётчик: если свип видит меньше эндпоинтов,
чем их заведомо есть, он падает сам.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_KEYS = {
    "api_key",
    "bot_token",
    "session_string",
    "password_hash",
    "openrouter_api_key",
    "telegram_bot_token",
    "api_hash",
    # ключ ответа «показать секрет» (9.14): чтобы новый эндпоинт не мог
    # начать отдавать удостоверение под этим именем незаметно
    "secret",
}

# Эндпоинты, которым показывать секрет владельцу разрешено ЯВНО (9.14).
# Список именной и короткий: каждое имя здесь — отдельное решение, а не
# режим работы. Показ идёт владельцу строки, за входом и CSRF, и оставляет
# запись в журнале; обычные ответы настроек секрета по-прежнему не несут.
DELIBERATE_REVEAL = {
    "reveal_openrouter_key",
    "reveal_bot_token",
    "_reveal",  # общее тело обоих
}

# Эндпоинтов в приложении заведомо больше трёх десятков. Точное число
# меняется с каждой задачей, поэтому проверяется порядок величины: свип,
# внезапно увидевший единицы, сломан — как это и случилось после 5.1.
MIN_ENDPOINTS_SEEN = 30


# Эндпоинт объявляется либо на приложении (`@app.get`, статические
# страницы в main.py), либо на роутере (`@router.get`, вся Фаза 5).
# Пропущенное второе имя и сделало проверку пустой.
DECORATOR_OWNERS = {"app", "router"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _route_functions(tree: ast.AST):
    """Функции-эндпоинты: декоратор @<app|router>.<http-метод>."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(call, ast.Attribute)
                and isinstance(call.value, ast.Name)
                and call.value.id in DECORATOR_OWNERS
                and call.attr in HTTP_METHODS
            ):
                yield node
                break


def _returned_keys(func: ast.AST) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _sources():
    """Весь исполняемый код. Монолит server.py удалён задачей 7.4; список
    каталогов вместо перечня файлов — новый роутер проверяется сам,
    без правки теста."""
    files = sorted((ROOT / "app").rglob("*.py"))
    files += sorted((ROOT / "scripts").rglob("*.py"))
    existing = [f for f in files if f.exists()]
    assert existing, "нечего сканировать — проверка выродилась в пустую"
    return existing


def test_no_endpoint_returns_a_raw_secret():
    violations = []
    seen = 0
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in _route_functions(tree):
            seen += 1
            if func.name in DELIBERATE_REVEAL:
                continue
            leaked = _returned_keys(func) & FORBIDDEN_KEYS
            if leaked:
                violations.append(
                    f"{path.name}:{func.lineno} {func.name}() → {sorted(leaked)}"
                )
    assert seen >= MIN_ENDPOINTS_SEEN, (
        f"свип нашёл всего {seen} эндпоинтов — он опять смотрит не туда "
        "(так он молча пропускал всю Фазу 5); проверьте DECORATOR_OWNERS"
    )
    assert not violations, "Эндпоинты возвращают секреты:\n" + "\n".join(violations)


def test_no_helper_at_the_http_boundary_returns_a_secret_either():
    """Свип по функциям-эндпоинтам видит только их собственные литералы.

    Обработчик, возвращающий `await _что_то()`, для него пуст — а секрет
    в ответе от этого никуда не девается. Поэтому на границе HTTP
    (`app/api/`) проверяется КАЖДАЯ функция, а не только помеченная
    декоратором. Сервисный слой сюда не входит осознанно: там сырые
    секреты и обязаны быть, он их шифрует и расшифровывает.
    """
    violations = []
    seen = 0
    for path in sorted((ROOT / "app" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seen += 1
            if node.name in DELIBERATE_REVEAL:
                continue
            leaked = _returned_keys(node) & FORBIDDEN_KEYS
            if leaked:
                violations.append(
                    f"{path.name}:{node.lineno} {node.name}() → {sorted(leaked)}"
                )
    assert seen >= MIN_ENDPOINTS_SEEN, f"на границе HTTP видно всего {seen} функций"
    assert not violations, "На границе HTTP секрет собирается в ответ:\n" + "\n".join(
        violations
    )


def test_deliberate_reveal_endpoints_still_exist():
    """Разрешение не должно пережить сам эндпоинт.

    Исключение, оставшееся в списке после удаления функции, — это тихо
    расширенное разрешение: имя освобождается, и однажды его займёт другой
    обработчик, унаследовав право отдавать секрет.
    """
    names = set()
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    stale = DELIBERATE_REVEAL - names
    assert not stale, f"исключение висит без эндпоинта: {sorted(stale)}"
