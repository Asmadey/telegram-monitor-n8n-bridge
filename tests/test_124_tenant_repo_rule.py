"""Правило `TenantRepo` и код обязаны сойтись (задача 12.6 `docs/PLAN.md`).

`AGENTS.md` §5 требует: «Не пишите `WHERE user_id = ...` руками. Ходите через
`TenantRepo` — слой, в котором забыть фильтр невозможно». В коде при этом
двадцать ручных мест. Каждое из них по-своему законно, но правило об этом
молчит — и любой, кто следует `AGENTS.md` буквально, видит двадцать нарушений
и не знает, можно ли ему так же. **Правило, которому код открыто не следует,
перестаёт быть правилом**: следующий ручной фильтр напишут не задумываясь, и
отличить законный от забытого будет уже нечем.

Здесь ручные места разделены на два вида, и граница проведена там, где она
что-то значит:

1. **`TenantRepo` на руках — фильтр писать нельзя.** Если в функции есть
   `repo`, то `repo.query(Model)` и `repo.delete(Model)` дают ровно тот же
   запрос, и написать его руками можно только по невнимательности. Забыть
   `where` в таком месте — значит отдать данные соседа.

2. **`TenantRepo` нет — фильтр писать можно, и список таких мест закреплён.**
   Это два семейства: службы (`app/services/`), которым `user_id` передают
   аргументом (их зовёт воркер, где запроса и репозитория нет вовсе), и
   доaвторизационные пути (`app/api/auth.py`, `app/api/telegram.py`), которые
   работают с собственными записями пользователя — сессия, попытка входа,
   привязка аккаунта, — а не с тенантными ресурсами.

Первое проверяется свойством кода и потому не устаревает. Второе —
перечислением, и рост списка обязан проходить через ревью.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

MANUAL_FILTER = "user_id =="

# Места, где ручной фильтр законен: репозитория на руках нет. Ключ — файл,
# значение — почему. Список закрыт: новый файл здесь — повод для разговора,
# а не для молчаливого добавления строки.
ALLOWED_WITHOUT_REPO = {
    "app/db.py": "сам TenantRepo — здесь фильтр и живёт",
    "app/api/auth.py": "сессии пользователя: собственная запись, не тенантный ресурс",
    "app/api/telegram.py": "привязка аккаунта и попытки входа — собственные записи",
    "app/services/llm.py": "служба, user_id приходит аргументом (зовёт воркер)",
    "app/services/tg_attempts.py": "служба, user_id приходит аргументом",
    "app/services/tg_account.py": "служба, user_id приходит аргументом",
    "app/services/integrations.py": "служба, user_id приходит аргументом",
}


def _functions_with_repo(tree: ast.AST) -> list[tuple[int, int]]:
    """Диапазоны строк функций, у которых `TenantRepo` на руках."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        source = ast.dump(node)
        has_repo = "'repo'" in source or "TenantRepo" in source
        if has_repo and node.end_lineno:
            spans.append((node.lineno, node.end_lineno))
    return spans


def manual_filters(source: str) -> list[tuple[int, str, bool]]:
    """(строка, текст, есть ли рядом repo) для каждого ручного фильтра."""
    tree = ast.parse(source)
    spans = _functions_with_repo(tree)
    found = []
    for number, line in enumerate(source.splitlines(), start=1):
        if MANUAL_FILTER not in line or line.strip().startswith("#"):
            continue
        with_repo = any(start <= number <= end for start, end in spans)
        found.append((number, line.strip(), with_repo))
    return found


# --------------------------------------------------------------------------
# Self-test'ы сканера
# --------------------------------------------------------------------------


def test_scanner_sees_a_manual_filter_where_a_repo_is_at_hand():
    src = (
        "async def list_things(repo: TenantRepo):\n"
        "    return await repo.db.scalars(select(T).where(T.user_id == repo.user_id))\n"
    )
    assert manual_filters(src) == [
        (
            2,
            "return await repo.db.scalars(select(T).where(T.user_id == repo.user_id))",
            True,
        )
    ]


def test_scanner_does_not_confuse_a_service_taking_user_id():
    src = "async def usage(db, user_id: int):\n    return select(U).where(U.user_id == user_id)\n"
    found = manual_filters(src)
    assert len(found) == 1 and found[0][2] is False, (
        f"служба принята за роутер: {found}"
    )


def test_scanner_ignores_a_commented_out_filter():
    src = "def f(repo):\n    # T.user_id == repo.user_id\n    return 1\n"
    assert manual_filters(src) == [], "комментарий засчитан за код"


# --------------------------------------------------------------------------
# Живой свип
# --------------------------------------------------------------------------


def _sweep() -> dict[str, list[tuple[int, str, bool]]]:
    out = {}
    for path in sorted(APP.rglob("*.py")):
        found = manual_filters(path.read_text(encoding="utf-8"))
        if found:
            out[path.relative_to(ROOT).as_posix()] = found
    return out


def test_sweep_actually_sees_the_code():
    """Антивакуум: пустая выборка зеленеет сама по себе."""
    total = sum(len(v) for v in _sweep().values())
    assert total >= 10, f"ручных фильтров найдено {total} — сканер ослеп"


def test_no_manual_filter_where_a_tenant_repo_is_at_hand():
    guilty = {
        name: [f"{number}: {text}" for number, text, with_repo in found if with_repo]
        for name, found in _sweep().items()
        if name != "app/db.py" and any(with_repo for _, _, with_repo in found)
    }
    assert not guilty, (
        "ручной WHERE user_id там, где на руках TenantRepo: repo.query(Model) и "
        f"repo.delete(Model) дают тот же запрос, забыть в них фильтр нельзя — {guilty}"
    )


def test_the_list_of_exceptions_does_not_grow_silently():
    files = set(_sweep())
    unexpected = files - set(ALLOWED_WITHOUT_REPO)
    assert not unexpected, (
        "ручной фильтр в файле, которого нет в списке исключений: если "
        f"репозитория там действительно нет, впишите его и объясните — {sorted(unexpected)}"
    )


def test_the_rule_in_agents_md_names_its_exception():
    """Правило без записанного исключения код опровергает, а не направляет."""
    src = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "TenantRepo" in src, "в AGENTS.md больше нет правила про TenantRepo"
    window = src[src.index("Не пишите `WHERE user_id") :][:1500]
    assert "app/services/" in window, (
        "правило не называет исключение: службы получают user_id аргументом, "
        "репозитория у них нет — это должно быть написано, а не подразумеваться"
    )
