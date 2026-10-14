"""Сущности и разметка внутри списков (задача 13.23).

Найдено 2026-10-14 на живой ленте после 13.20: в сводке «ТОП-вакансий» стояло
`P&amp;amp;L` — пользователь видел «P&amp;L» и в кабинете, и в боте. Модель
отвечает настоящим HTML, где амперсанд уже записан сущностью `&amp;`, а разбор
экранировал текст между тегами ещё раз.

Второе — в том же разборе: список и таблица убираются в заглушку РАНЬШЕ, чем
применяются ссылки и жирный, поэтому `- [Вакансия](https://…)` приезжал
буквами, хотя та же ссылка вне списка становилась ссылкой.
"""

from app.services.telegram_markup import to_cabinet_text, to_telegram_html


def test_an_entity_from_the_model_is_not_escaped_twice():
    out = to_telegram_html("Ответственность за P&amp;L и R&amp;D")
    assert "&amp;amp;" not in out, out
    assert "P&amp;L" in out, out


def test_a_bare_ampersand_is_still_escaped_once():
    assert to_telegram_html("AT&T") == "AT&amp;T"


def test_an_escaped_tag_stays_text():
    """Антивакуум: раскрыть сущность и снова экранировать — не то же самое,
    что раскрыть и оставить. `&lt;script&gt;` обязан остаться текстом."""
    out = to_telegram_html("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert "<script" not in out, out
    assert "&lt;script&gt;" in out, out


def test_links_and_bold_inside_a_list_item_become_markup():
    out = to_telegram_html("- [Вакансия](https://t.me/c/1/2) — 100\n- **Жирный** пункт")
    assert '<li><a href="https://t.me/c/1/2">Вакансия</a> — 100</li>' in out, out
    assert "<li><b>Жирный</b> пункт</li>" in out, out


def test_links_and_bold_inside_a_table_cell_become_markup():
    out = to_telegram_html(
        "| Роль | Пост |\n|---|---|\n| **PM** | [пост](https://t.me/c/1) |"
    )
    assert "<td><b>PM</b></td>" in out, out
    assert '<td><a href="https://t.me/c/1">пост</a></td>' in out, out


def test_cabinet_text_shows_the_ampersand_once():
    assert to_cabinet_text("P&amp;L") == "P&L"
