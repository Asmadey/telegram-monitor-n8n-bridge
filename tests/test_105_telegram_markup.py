"""Разметка доезжает до Telegram разобранной (9.17).

Найдено владельцем: анализ канала «AI Engineers Jobs» приходит в бота с
сырыми звёздочками — `**Ключевая суть**`, `` `#AI` ``, `### Роли`, — а
для «Finder.work» всё оформлено правильно. Владелец предположил, что дело
в промптах, и оказался прав: у Finder.work промпт на 4311 символов и
прямо требует HTML с примерами `<b>`, у остальных четырёх каналов промпт
пустой или ничего про разметку не говорит. Модель по умолчанию отвечает
Markdown, а доставка шлёт с `parse_mode=HTML` — Telegram показывает
звёздочки как текст.

Чинить это правкой промптов нельзя, и вот почему. Промпт — не гарантия:
модель отклоняется от него на длинных ответах, меняется между версиями, а
писать промпты будут пользователи, которым знать про подмножество HTML в
Bot API неоткуда. Формат ответа — обязанность доставки, а не автора
промпта.

Поэтому текст нормализуется перед отправкой: Markdown переводится в теги,
уже готовый HTML остаётся как есть, а всё остальное экранируется.

Последнее — не косметика. Пост со знаком `<` ломал разбор, Bot API
отвечал 400, и доставка повторяла запрос БЕЗ `parse_mode` — сообщение
уходило, но целиком без оформления. То есть один символ в чужом посте
незаметно отключал разметку всему сообщению.
"""

import pytest

from app.services.telegram_markup import to_telegram_html


def test_markdown_bold_becomes_a_tag():
    out = to_telegram_html("📌 **Ключевая суть**: подборка вакансий")
    assert "<b>Ключевая суть</b>" in out
    assert "**" not in out, f"звёздочки доехали до Telegram как текст: {out!r}"


def test_headers_and_inline_code_and_links():
    out = to_telegram_html(
        "### Роли\nТехнологии: `Python`, `vLLM`\n[Вакансия](https://example.com/job)"
    )
    # Было `<b>Роли</b>`: пока доставка шла через `sendMessage`, заголовков
    # в её HTML не существовало, и жирный был лучшим приближением. С
    # переходом на `sendRichMessage` (9.19) заголовок стал настоящим тегом —
    # уровень виден глазом, а не только жирностью. Запасной путь
    # разворачивает его обратно в `<b>`.
    assert "<h3>Роли</h3>" in out, f"заголовок остался решётками: {out!r}"
    assert "<code>Python</code>" in out
    assert '<a href="https://example.com/job">Вакансия</a>' in out
    assert "#" not in out.split("\n")[0]


def test_ready_html_is_left_alone():
    """Промпт Finder.work уже требует HTML — его вывод трогать нельзя."""
    source = "<b>Вакансии: продажи</b>\n<a href='https://t.me/c/1'>🔗 Источник</a>"
    out = to_telegram_html(source)
    assert "<b>Вакансии: продажи</b>" in out
    assert "&lt;b&gt;" not in out, f"готовый HTML экранирован дважды: {out!r}"
    assert "🔗 Источник</a>" in out


def test_stray_angle_bracket_is_escaped_not_sent_raw():
    """Один `<` в чужом посте отключал разметку всему сообщению."""
    out = to_telegram_html("Зарплата 5 < 10 тысяч & премия")
    assert "&lt;" in out and "&amp;" in out, f"не экранировано: {out!r}"


def test_unsupported_tags_are_escaped():
    """Bot API знает короткий список тегов; остальное — текст, а не разметка."""
    out = to_telegram_html("<div onclick='x'>привет</div><script>alert(1)</script>")
    assert "<div" not in out and "<script" not in out, (
        f"чужой тег ушёл в Telegram как разметка: {out!r}"
    )
    assert "&lt;div" in out


def test_line_break_tag_becomes_a_newline():
    """`<br>` Bot API не знает, но и мусором в тексте он быть не должен."""
    out = to_telegram_html("первая<br>вторая<br/>третья")
    assert "<br" not in out and "&lt;br" not in out, f"остался br: {out!r}"
    assert out.count("\n") == 2


def test_markdown_inside_code_stays_text():
    out = to_telegram_html("`**не жирный**`")
    assert "<code>**не жирный**</code>" in out, (
        f"внутри кода Markdown разобран, хотя это буквальный текст: {out!r}"
    )


def test_fenced_block_becomes_pre():
    out = to_telegram_html("```python\nprint(1)\n```")
    assert "<pre>" in out and "print(1)" in out


@pytest.mark.asyncio
async def test_bot_receives_normalised_text(db, user):
    """Нормализация стоит на пути доставки, а не остаётся утилитой."""
    from test_57_dispatch import PAYLOAD, Recorder, _enable_all

    from app.services.dispatch import dispatch

    await _enable_all(db, user.id)
    bot = Recorder(result=True)

    await dispatch(
        db,
        user.id,
        dict(PAYLOAD),
        llm_caller=Recorder(result=("**Ключевая суть**: вакансии", 120)),
        bot_sender=bot,
        webhook_sender=Recorder(result=200),
    )

    assert bot.calls, "бот не получил сообщение"
    sent = bot.calls[0][0][2]
    assert "<b>Ключевая суть</b>" in sent, f"в бота ушёл сырой Markdown: {sent!r}"


def test_summary_escapes_foreign_text():
    """Сводку без анализа собираем мы — из чужих постов и чужих заголовков."""
    from app.services.dispatch import _summary_text

    out = _summary_text(
        "Канал <script>",
        [{"text": "цена 5 < 10 & дешевле", "post_url": "https://t.me/c/1"}],
    )
    assert "<script>" not in out, f"заголовок канала ушёл разметкой: {out!r}"
    assert "&lt;" in out and "&amp;" in out


def test_chunking_never_splits_a_tag():
    """Резать по 3900 символов вслепую — значит однажды разрубить тег."""
    from app.services.dispatch import BOT_CHUNK, _chunks

    text = ("а" * (BOT_CHUNK - 1)) + "<b>жирный кусок текста</b>" + ("б" * 200)
    for chunk in _chunks(text):
        assert chunk.count("<") == chunk.count(">"), (
            f"тег разрублен на границе куска: {chunk[-40:]!r}"
        )
