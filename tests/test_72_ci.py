"""Задача 7.3 — CI как обязательный гейт (PLAN.md раздел 10).

Четыре джоба по образцу Rails-шаблона (Ruby/.github/workflows/ci.yml):
security = bandit + pip-audit (аналог brakeman), lint = ruff, typecheck = mypy,
test = pytest. Плюс собственный secret-scan.

Два требования, которые легко потерять и которые здесь закреплены тестом.

1. Секрет-скан идёт по ВСЕМУ дереву и по истории, а не по списку расширений.
   AGENTS.md §7: узкая проверка даёт зелёный статус при живом ключе в
   репозитории, и это хуже, чем отсутствие проверки. Метрика, сканировавшая
   только .py/.env/.yml, уже однажды пропустила пароли в markdown-файле.

2. Джоб test поднимает Postgres и отдаёт TEST_DATABASE_URL. Без него три
   поведенческих теста (Alembic 1.4, перенос данных 1.5) честно пишут skip —
   и задачи остаются неподтверждёнными навсегда. AGENTS.md §4: вердикт
   выносит прогон в среде, где база есть. Для этого репозитория такая
   среда — CI.
"""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_JOBS = {"security", "lint", "typecheck", "test", "secret-scan"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job_text(job: dict) -> str:
    """Всё, что джоб исполняет и объявляет — одной строкой для поиска."""
    return yaml.safe_dump(job, allow_unicode=True)


def test_workflow_exists():
    assert WORKFLOW.exists(), f"нет {WORKFLOW.relative_to(ROOT)}"


def test_runs_on_pull_request_and_main():
    wf = _workflow()
    # ключ `on` YAML разбирает как булево True — это известная ловушка
    triggers = wf.get("on", wf.get(True))
    assert triggers, "не объявлены триггеры запуска"
    assert "pull_request" in triggers, "CI не запускается на pull request"
    push = triggers.get("push") or {}
    assert "main" in (push.get("branches") or []), "CI не запускается на push в main"


def test_all_four_jobs_plus_secret_scan_present():
    jobs = set(_workflow()["jobs"])
    missing = REQUIRED_JOBS - jobs
    assert not missing, f"нет джобов: {sorted(missing)}"


def test_security_job_runs_bandit_and_pip_audit():
    text = _job_text(_workflow()["jobs"]["security"])
    assert "bandit" in text, "security не запускает bandit"
    assert "pip-audit" in text or "pip_audit" in text, "security не запускает pip-audit"


def test_lint_job_checks_both_rules_and_format():
    text = _job_text(_workflow()["jobs"]["lint"])
    assert "ruff check" in text, "lint не запускает ruff check"
    assert "ruff format" in text, "lint не проверяет форматирование"


def test_typecheck_job_runs_mypy_on_app():
    text = _job_text(_workflow()["jobs"]["typecheck"])
    assert "mypy" in text, "typecheck не запускает mypy"


def test_test_job_provides_live_postgres():
    """Без сервиса Postgres поведенческие тесты 1.4/1.5 уходят в skip
    и задачи навсегда остаются неподтверждёнными."""
    job = _workflow()["jobs"]["test"]
    services = job.get("services") or {}
    assert any("postgres" in str(v.get("image", "")) for v in services.values()), (
        "джоб test не поднимает сервис Postgres — поведенческие тесты уйдут в skip"
    )
    assert "TEST_DATABASE_URL" in _job_text(job), (
        "джоб test не отдаёт TEST_DATABASE_URL — тесты 1.4/1.5 не увидят базу"
    )


def test_test_job_runs_pytest():
    assert "pytest" in _job_text(_workflow()["jobs"]["test"])


def test_secret_scan_covers_whole_tree_and_history():
    """AGENTS.md §7: скан по списку расширений — ложное спокойствие."""
    text = _job_text(_workflow()["jobs"]["secret-scan"])
    assert "gitleaks" in text.lower() or "trufflehog" in text.lower(), (
        "secret-scan не запускает ни gitleaks, ни trufflehog"
    )
    assert "fetch-depth: 0" in text or "fetch-depth: '0'" in text, (
        "без полной истории (fetch-depth: 0) скан видит только последний коммит"
    )


def test_ci_python_matches_the_shipped_image():
    """CI обязан собирать тот же Python, что и образ.

    Найдено при ревью 7.3: Dockerfile объявлял 3.11, mypy.ini — 3.12,
    локальное окружение — 3.13. Проверка типов шла против языка, которого
    в проде нет; расхождение проявилось бы уже на живом деплое.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    image_version = dockerfile.split("FROM python:")[1].split("-")[0].strip()

    wf_text = WORKFLOW.read_text(encoding="utf-8")
    assert f'"{image_version}"' in wf_text or f"'{image_version}'" in wf_text, (
        f"CI не пинит python {image_version} — версию образа"
    )

    mypy_ini = (ROOT / "mypy.ini").read_text(encoding="utf-8")
    assert f"python_version = {image_version}" in mypy_ini, (
        f"mypy проверяет не ту версию языка: образ несёт {image_version}"
    )


def test_every_job_installs_dev_requirements():
    """Джоб, забывший зависимости, падает с ImportError и читается как
    поломка кода, а не как дыра в конфиге."""
    jobs = _workflow()["jobs"]
    for name in ("security", "lint", "typecheck", "test"):
        text = _job_text(jobs[name])
        assert "requirements-dev.txt" in text, f"джоб {name} не ставит зависимости"
