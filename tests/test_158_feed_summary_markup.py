"""Сводка источника в кабинете — оформленной, как в боте (задача 13.20).

Находка владельца 2026-10-13, вкладка «Лента»: у источников в режиме prompt
`ai_analysis` — HTML для Telegram (промпт ответа просит HTML, доставка
приводит его `to_telegram_html`). Кабинет рисовал поле через
`formatTelegramText`, который экранирует всё, и пользователь видел буквальные
`<b>…</b>` и `<a href="https://t.me/...">` — в 7 карточках из 8 последних.

Решение (а): сервер отдаёт `ai_analysis_html` — выход ТОГО ЖЕ санитайзера,
что и у бота, плюс `rel`/`target` у ссылок, — и `ai_analysis_text` для
сниппета карточки. Один санитайзер: кабинет и бот не разъедутся.

Текст — выход модели, собранный из чужих постов: враждебный.
"""

from app.api.feed import _card
from app.models import FeedItem

HOSTILE = (
    '<b>x</b><a href="https://t.me/c/1">y</a>'
    "<img src=x onerror=alert(1)>"
    '<a href="javascript:alert(1)">z</a>'
)


def _item(analysis: str) -> FeedItem:
    return FeedItem(
        id=1,
        user_id=1,
        chat_id=0,
        chat_title="Источник",
        messages_count=1,
        ai_analysis=analysis,
        delivery_status="ok",
    )


def test_card_carries_sanitized_html_rendered_like_the_bot():
    out = _card(_item(HOSTILE)).get("ai_analysis_html") or ""
    assert "<b>x</b>" in out, f"жирный не дошёл: {out!r}"
    assert (
        '<a href="https://t.me/c/1" rel="noopener noreferrer" target="_blank">y</a>'
        in out
    )
    assert "<img" not in out and "&lt;img" in out, f"img жив: {out!r}"
    assert "javascript:" not in out.replace("&lt;a href=&quot;javascript:", ""), out
    assert 'href="javascript' not in out


def test_markdown_link_cannot_inject_an_attribute():
    """`[x](https://a"onmouseover="…)`: текст экранирован с quote=False, и
    кавычка из адреса уходила в href сырой — новый атрибут в браузере."""
    out = (
        _card(_item('[x](https://a"onmouseover="location=name)')).get(
            "ai_analysis_html"
        )
        or ""
    )
    assert out, "поля ai_analysis_html нет"
    assert '"onmouseover' not in out, f"атрибут внедрён: {out!r}"


def test_card_snippet_is_plain_text():
    out = _card(_item(HOSTILE)).get("ai_analysis_text")
    assert out is not None, "поля ai_analysis_text нет"
    assert "<b>" not in out and "</a>" not in out, out
    assert out.startswith("xy"), out


# --- кабинет: вставка только проверенной сервером разметки -----------------
#
# XSS-сканер test_02 смотрит на подстановки `${…}`, а прямое присваивание
# `innerHTML = поле` он не видит. Поэтому доверие к `ai_analysis_html`
# закреплено здесь отдельно: в разметку сводка попадает либо готовой из
# санитайзера сервера, либо через экранирующий `formatTelegramText`.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

FEED_JS = (Path(__file__).resolve().parents[1] / "static" / "js" / "feed.js").read_text(
    encoding="utf-8"
)


def test_detail_inserts_only_server_sanitized_markup():
    assert "feedDetailSummary.innerHTML = item.ai_analysis_html" in FEED_JS, (
        "подробный вид не берёт сводку, проверенную сервером"
    )
    for expr in re.findall(r"innerHTML\s*=\s*([^;]+);", FEED_JS):
        if "ai_analysis" in expr and "ai_analysis_html" not in expr:
            assert "formatTelegramText(" in expr, (
                f"сырой ai_analysis уходит в разметку без экранирования: {expr!r}"
            )


def test_card_snippet_uses_server_plain_text():
    assert "item.ai_analysis_text" in FEED_JS, "сниппет не берёт простой текст сервера"
    assert "item.ai_analysis.replace(" not in FEED_JS, (
        "сниппет снова чистит HTML на клиенте — второй разбор того же текста"
    )
