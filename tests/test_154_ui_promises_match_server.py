"""Интерфейс не обещает того, чего нет (13.18).

Два одинаковых по природе дефекта, найденных сверкой колоды с кодом:

1. Окно автоочистки предлагает срок «180 дней (полгода)», а сервер принимает
   только 7/14/30/60/90 и на 180 отвечает 400. Вариант пришёл из интерфейса
   монолита, а список сервера писали «теми же вариантами» — и пропустили
   один. Человек выбирает пункт из предложенного списка и получает отказ.
2. Подсказки отправляют во вкладку «Каналы», которой нет с задачи 11.7: она
   называется «Источники». Свип 11.8 искал слово «монитор» и ссылку на
   вкладку не видел.

Обе проверки сторожат стык, а не значение: список сроков в разметке обязан
быть подмножеством списка сервера, имя вкладки в тексте — именем настоящей
кнопки вкладки. Новый срок или переименование вкладки попадут под проверку
сами.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import act_as

from app.api.cleanup import ALLOWED_DAYS

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
JS_DIR = ROOT / "static" / "js"

TAB_REFERENCE = re.compile(r"вкладк[а-яё]*\s*(?:<b>)?«([^»]+)»", re.IGNORECASE)


class _Collector(HTMLParser):
    """Варианты срока в окне автоочистки и подписи кнопок вкладок."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanup_days: list[int] = []
        self.tab_labels: set[str] = set()
        self._in_cleanup_select = False
        self._in_tab_button = False
        self._tab_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select" and a.get("id") == "cleanupDaysSelect":
            self._in_cleanup_select = True
        elif tag == "option" and self._in_cleanup_select:
            self.cleanup_days.append(int(a["value"]))
        elif tag == "button" and "tab-btn" in (a.get("class") or "").split():
            self._in_tab_button = True
            self._tab_text = []
        elif tag == "span" and self._in_tab_button:
            # счётчик рядом с подписью — не часть имени вкладки
            self._in_tab_button = "label-done"

    def handle_endtag(self, tag):
        if tag == "select":
            self._in_cleanup_select = False
        elif tag == "button" and self._in_tab_button:
            self.tab_labels.add(" ".join("".join(self._tab_text).split()))
            self._in_tab_button = False

    def handle_data(self, data):
        if self._in_tab_button is True:
            self._tab_text.append(data)


def _parsed() -> _Collector:
    collector = _Collector()
    collector.feed(INDEX.read_text(encoding="utf-8"))
    return collector


def _visible_text(path: Path) -> str:
    """Текст без комментариев: в комментариях история названий уместна."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def test_parser_sees_the_real_markup():
    """Self-test: без него пустой разбор дал бы зелёный на любом файле."""
    parsed = _parsed()
    assert parsed.cleanup_days, "не найден список сроков #cleanupDaysSelect"
    assert {"Лента", "Источники", "Интеграция"} <= parsed.tab_labels, parsed.tab_labels


def test_every_offered_retention_is_accepted_by_the_server():
    offered = _parsed().cleanup_days
    refused = [days for days in offered if days not in ALLOWED_DAYS]
    assert not refused, (
        f"окно автоочистки предлагает сроки {refused}, которые сервер отклонит "
        f"с 400 (допустимы {ALLOWED_DAYS})"
    )


def test_hints_name_only_tabs_that_exist():
    tabs = _parsed().tab_labels
    stale = []
    for path in [INDEX, *sorted(JS_DIR.glob("*.js"))]:
        for name in TAB_REFERENCE.findall(_visible_text(path)):
            if name not in tabs:
                stale.append(f"{path.name}: «{name}»")
    assert not stale, (
        f"подсказки отправляют во вкладки, которых нет (есть: {sorted(tabs)}):\n"
        + "\n".join(stale)
    )


@pytest.mark.asyncio
async def test_half_year_retention_is_saved(anon_client, db, user):
    await act_as(anon_client, db, user)
    resp = await anon_client.post("/api/cleanup", json={"enabled": True, "days": 180})
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"] == 180
