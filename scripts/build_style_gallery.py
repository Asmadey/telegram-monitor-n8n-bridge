"""Галерея экранов: один файл, который можно открыть двойным щелчком.

Зачем. Живого кабинета у агента нет: локально приложение не поднимается
(`APP_ENCRYPTION_KEY` и `DATABASE_URL` живут в переменных Railway), а боевой
кабинет за входом. Значит правку внешнего вида некому посмотреть — а это
правка, которую только глазами и проверяют.

Галерея собирается из НАСТОЯЩИХ исходников: разметка берётся из
`static/index.html`, стили — из `static/css/main.css`, строки каналов строит
тот же `channelRowMarkup` из `static/js/render.js`, что и в бою. Подставные
здесь только данные. Галерея, нарисованная своей разметкой, показывала бы не
кабинет, а саму себя — та же ошибка, что тест, сеющий данные, которых писатель
не пишет (11.9).

Файл самодостаточный: CSS и скрипт вшиты внутрь, сеть не нужна, `file://`
достаточно. В образ не уезжает — пишется в служебный каталог.

    .venv/bin/python -m scripts.build_style_gallery [куда.html]
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
CSS = ROOT / "static" / "css" / "main.css"
RENDER_JS = ROOT / "static" / "js" / "render.js"

DEFAULT_OUT = ROOT / "private-backups" / "style-gallery.html"

# Подставные каналы: пять состояний проверки (13.6) — чтобы на одном экране
# были видны все цвета значков, а не только зелёный.
CHANNELS = """[
  {channel_id: 1, chat_title: 'Job for Products and Projects', chat_target: '@forproducts',
   limit: 20, token_limit: 0, extract_prompt: 'Отбирать вакансии продуктовых менеджеров',
   check_status: 'ok', check_detail: 'канал «Job for Products and Projects» — читается'},
  {channel_id: 2, chat_title: 'TOP LEVEL JOB [топ вакансии для руководителей]', chat_target: '@toplevel',
   limit: 20, token_limit: 0, extract_prompt: 'Отбирать вакансии уровня директора',
   check_status: 'duplicate', check_detail: 'канал уже есть в источнике как «@forproducts» — опрашивался бы дважды'},
  {channel_id: 3, chat_title: 'g_jobbot', chat_target: '@g_jobbot',
   limit: 20, token_limit: 5000, extract_prompt: '',
   check_status: 'no_access', check_detail: 'бот «g_jobbot» найден, но история не читается: обычно это значит, что аккаунт не состоит в нём'},
  {channel_id: 4, chat_title: 'Вакансии директоров, менеджеров в IT', chat_target: '@itdir',
   limit: 50, token_limit: 0, extract_prompt: 'Отбирать управленческие роли',
   check_status: null, check_detail: null},
  {channel_id: 5, chat_title: null, chat_target: '-1004324020362',
   limit: 20, token_limit: 0, extract_prompt: '',
   check_status: 'not_found', check_detail: 'адрес не открылся: Cannot find any entity'}
]"""

LABEL_CSS = """
    /* Только для галереи: подписи разделов и раскладка «всё сразу». */
    .gallery-label {
      margin: 32px 0 10px;
      padding: 6px 10px;
      background: #080808;
      color: #fff;
      border-radius: 4px;
      font: 500 12px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.6px;
      text-transform: uppercase;
    }
    .tab-pane { display: block !important; }
    .modal-overlay.gallery-open {
      position: static;
      display: block;
      background: transparent;
      padding: 0;
    }
"""


def build() -> str:
    html = INDEX.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    render_js = RENDER_JS.read_text(encoding="utf-8")

    # 1. Стили внутрь: галерея открывается с диска, где /static/ не отдаётся.
    html = re.sub(
        r'<link rel="stylesheet" href="/static/css/main\.css">',
        f"<style>{css}{LABEL_CSS}</style>",
        html,
        count=1,
    )
    # 2. Боевой модуль убираем целиком: он сразу пойдёт в API, которого нет.
    html = html.replace('<script type="module" src="/static/js/main.js"></script>', "")
    # 3. Оболочка показывается: в бою её раскрывает вход.
    html = html.replace('id="appShell" hidden', 'id="appShell"')
    # 4. Каждая вкладка получает подпись — иначе пять экранов подряд
    #    неразличимы, а смотреть их надо именно подряд.
    for pane, title in (
        ("tab-feed", "Лента"),
        ("tab-messages", "Сообщения"),
        ("tab-sources", "Источники"),
        ("tab-integration", "Интеграции"),
        ("tab-logs", "Журнал"),
    ):
        html = html.replace(
            f'<div id="{pane}"',
            f'<div class="gallery-label">{title}</div><div id="{pane}"',
            1,
        )

    filler = f"""
<script type="module">
{render_js}

// Строки каналов строит БОЕВОЙ билдер — подставные здесь только данные.
const channels = {CHANNELS};
const list = document.getElementById('editChannelsList');
if (list) list.innerHTML = channels.map(channelRowMarkup).join('');
const count = document.getElementById('editChannelsCount');
if (count) count.textContent = String(channels.length);

// Четвёртая строка раскрыта — режим правки; третья спрашивает об удалении.
const rows = document.querySelectorAll('[data-channel-row]');
if (rows[3]) rows[3].querySelector('.channel-row-edit').hidden = false;
if (rows[2]) {{
  rows[2].querySelector('.channel-row-actions').hidden = true;
  rows[2].querySelector('.channel-row-confirm').hidden = false;
}}

// Модалка источника показывается в потоке страницы: она и есть экран,
// который правился последним.
const modal = document.getElementById('sourceModal');
if (modal) {{
  modal.classList.add('gallery-open');
  const label = document.createElement('div');
  label.className = 'gallery-label';
  label.textContent = 'Параметры источника (модальное окно)';
  modal.parentNode.insertBefore(label, modal);
}}
</script>
"""
    return html.replace("</body>", filler + "</body>")


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8")
    print(f"галерея собрана: {out} ({out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
