"""Идентификатор канала у API и у интерфейса — один и тот же (9.10).

Найдено владельцем: кнопка «Редактировать» не открывает модалку, а
переключатель мониторинга не показывает подтверждения.

Обе поломки — одна причина. API отдаёт карточку канала с ключом
`public_id` (переименование Фазы 1: `id` в новой схеме — внутренний BIGINT,
наружу он не уходит). Интерфейс остался с монолитным `m.id`, которого в
ответе нет. Дальше по цепочке:

- `data-monitor-id="${m.id}"` превращается в строку `"undefined"`;
- `toggleMonitor("undefined")` шлёт PATCH на `/api/monitors/undefined`,
  получает 404, и подтверждения не показывает — оно за `res.ok`;
- `openEditModal("undefined")` не находит канал и молча выходит.

Почему это не всплыло раньше: до задачи 9.8 обработчики были инлайновыми и
CSP их не исполняла — кнопки не работали ВООБЩЕ. Починка обработчиков не
сломала ничего, она обнажила следующий дефект, ждавший под ним.

Тесты API этого поймать не могли: там всё верно. Тесты интерфейса
проверяли наличие кнопок. Разошлись не части, а их СТЫК — и проверять надо
именно его: имя поля, которым интерфейс адресует канал.
"""

import pathlib
import re

import pytest
from conftest import act_as

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANNELS_JS = (ROOT / "static" / "js" / "channels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_interface_addresses_monitors_by_the_key_api_returns():
    """`m.id` в карточке канала — поле, которого в ответе нет."""
    offenders = [
        line.strip()[:100]
        for line in CHANNELS_JS.splitlines()
        if re.search(r"\bm\.id\b|item\.id\b|\bdata\.id\b", line)
    ]
    assert not offenders, (
        "интерфейс адресует канал полем, которого API не отдаёт "
        "(нужен public_id):\n" + "\n".join(offenders)
    )
    assert 'data-monitor-id="${m.public_id}"' in CHANNELS_JS, (
        "кнопки строки канала не получают идентификатор"
    )


@pytest.mark.asyncio
async def test_api_answers_with_public_id_and_nothing_else(anon_client, db, user):
    """Ответ несёт ровно тот ключ, которым интерфейс потом адресует канал."""
    from app.main import app
    from app.api.monitors import get_entity_resolver

    class _Entity:
        id = -1001234567890
        title = "Тестовый канал"
        username = "example"

    app.dependency_overrides[get_entity_resolver] = lambda: (
        lambda target: _entity_result()
    )

    async def _entity_result():
        return _Entity()

    await act_as(anon_client, db, user)
    try:
        created = await anon_client.post(
            "/api/monitors", json={"chat_target": "@example", "interval_minutes": 60}
        )
    finally:
        app.dependency_overrides.pop(get_entity_resolver, None)

    assert created.status_code in (200, 201), created.text
    body = created.json()
    assert body.get("public_id"), "в ответе нет public_id — адресовать канал нечем"
    assert "id" not in body, (
        "в ответе появился `id` — интерфейс снова начнёт адресовать им, "
        "а это внутренний ключ строки, наружу он не уходит"
    )


def test_twelve_hours_is_offered_everywhere_intervals_are_chosen():
    """Интервал 12 часов — и при добавлении, и при редактировании.

    Два независимых списка в разметке уже расходились по смыслу один раз;
    проверка держит их одинаковыми, иначе канал, созданный с интервалом,
    которого нет в модалке, при первом же редактировании молча съедет на
    соседнее значение.
    """
    selects = re.findall(
        r'<select id="(?:intervalMin|editIntervalMin)">(.*?)</select>',
        INDEX_HTML,
        re.S,
    )
    assert len(selects) == 2, "списков интервалов должно быть два: создание и правка"
    for options in selects:
        values = set(re.findall(r'value="(\d+)"', options))
        assert "720" in values, "нет варианта «каждые 12 часов»"
    assert set(re.findall(r'value="(\d+)"', selects[0])) == set(
        re.findall(r'value="(\d+)"', selects[1])
    ), "списки интервалов разошлись между формой создания и модалкой правки"
