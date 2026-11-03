"""Кнопка «Разобрать заново» на карточке источника (задача 13.30).

Сброс отметок «прочитано» есть на сервере давно (`reset-dedup`), а кнопки к
нему не было: применить исправленный промпт к уже прочитанным постам из
кабинета было нельзя. Кнопка сбрасывает отметки источника и тут же запускает
прогон, а перед этим говорит, чем это обернётся: повторные сообщения в бот и
n8n и повторный расход токенов. Прочитаны заново будут только посты из окна
канала (лимит и глубина в часах), более старые не вернутся.
"""

from pathlib import Path

JS = (
    Path(__file__).resolve().parent.parent / "static" / "js" / "sources.js"
).read_text(encoding="utf-8")


def _handler() -> str:
    start = JS.find("async function reparseSource")
    assert start != -1, "на карточке источника нет «Разобрать заново»"
    return JS[start : JS.find("\n}\n", start)]


def test_the_card_offers_to_reparse():
    assert 'data-action="reparse"' in JS, "кнопки «Разобрать заново» на карточке нет"
    assert "el.dataset.action === 'reparse'" in JS, "кнопка ни к чему не привязана"


def test_reparse_resets_the_marks_and_then_runs():
    handler = _handler()
    # Прогон — через `runSource`: он ждёт исхода и называет его (13.26);
    # второй, голый вызов `/run` дублировал бы это ожидание.
    reset, run = handler.find("/reset-dedup"), handler.find("runSource(")
    assert reset != -1, "кнопка не сбрасывает отметки «прочитано»"
    assert run != -1, "после сброса прогон не запускается — посты так и лежат"
    assert reset < run, "прогон раньше сброса разобрал бы пустоту"


def test_reparse_warns_about_repeats_and_tokens():
    handler = _handler()
    assert "confirm(" in handler, "сброс без подтверждения — повторы без спроса"
    assert "повторно" in handler and "токен" in handler, (
        "подтверждение не говорит, что сообщения придут повторно и токены уйдут заново"
    )
