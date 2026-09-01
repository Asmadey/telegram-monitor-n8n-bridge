"""Задача 0.3 — HTTP-эндпоинты не возвращают секреты в открытом виде.

GET /api/openrouter и GET /api/telegram-forward отдавали сырой api_key и
bot_token рядом с маской. Проверка статическая: разбираем AST и смотрим, что
именно возвращают функции, помеченные декоратором @app.<метод>.
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
}


def _route_functions(tree: ast.AST):
    """Функции с декоратором @app.get / @app.post / ... — то есть эндпоинты."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(call, ast.Attribute)
                and isinstance(call.value, ast.Name)
                and call.value.id == "app"
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
    files = [ROOT / "server.py"]
    files += sorted((ROOT / "app").rglob("*.py")) if (ROOT / "app").exists() else []
    return [f for f in files if f.exists()]


def test_no_endpoint_returns_a_raw_secret():
    violations = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in _route_functions(tree):
            leaked = _returned_keys(func) & FORBIDDEN_KEYS
            if leaked:
                violations.append(f"{path.name}:{func.lineno} {func.name}() → {sorted(leaked)}")
    assert not violations, "Эндпоинты возвращают секреты:\n" + "\n".join(violations)
