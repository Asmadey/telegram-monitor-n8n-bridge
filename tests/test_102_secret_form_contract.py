"""Пустое поле секрета — «не менял», а не «сотри» (9.13).

Найдено владельцем на живом деплое 8 сентября: ключ OpenRouter и токен бота
не сохранялись. Форма отвечала 200, всплывало «Настройки сохранены», в
журнале появлялась запись SETTINGS/SUCCESS — а `has_key` в базе оставался
`false`, и тест бота отвечал «Токен бота не задан».

Причина — в стыке двух половин одного контракта.

Серверная половина различает три состояния (С16, задача 0.3):
поле отсутствует — не трогать, `""` — очистить, значение — записать.
Она покрыта тестами и работает. Интерфейсная половина всегда слала
`input.value.trim()`, а поле после загрузки страницы ПУСТОЕ по построению:
сырой секрет наружу не отдаётся (К3), подставить его обратно нечем. То есть
каждое сохранение и каждое переключение тумблера отправляло `""` — команду
«сотри» — и стирало только что записанный секрет.

Почему это пережило и код-ревью, и зелёные тесты: `test_53_integrations.py`
проверял ровно то, что «форма сохраняется с пустым полем ключа — ключ
уцелеет», но проверял это со стороны сервера, посылая запрос БЕЗ поля.
Предположение о том, что интерфейс поле опустит, осталось предположением.
Контракт двух сторон, проверенный с одной стороны, — не проверенный
контракт.

Поэтому правило здесь формулируется так, чтобы его нельзя было нарушить
случайно: секретное поле не пишется в тело запроса напрямую вовсе. Оно
попадает туда только через `withSecret`, которая пустое значение
опускает, а осознанная очистка — отдельное действие с явным `""`.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"
SECRETS_JS = JS_DIR / "secrets.js"

# Имена секретных полей запроса — те, что бэкенд трактует по правилу
# None / "" / значение (app/api/integrations.py).
SECRET_FIELDS = ("api_key", "bot_token", "webhook_url")

# Поля ввода, в которые секрет вводят. Сырое значение в них не приходит
# никогда, поэтому пустота — норма, а не признак «пользователь стёр».
SECRET_INPUTS = ("openrouterApiKey", "tgBotToken", "webhookUrlInput")


def _js_sources() -> list[pathlib.Path]:
    return [
        p
        for p in sorted(JS_DIR.rglob("*.js"))
        if "vendor" not in p.parts and p.name != "secrets.js"
    ]


def test_no_form_puts_a_secret_field_into_a_request_body_directly():
    """Секрет уходит на сервер только через `withSecret`.

    Проверяется отсутствие ключа в литерале тела запроса, а не «правильность»
    выражения справа: в channels.js значение шло через промежуточную
    переменную (`const url = webhookUrlInput.value.trim()`), и проверка
    выражения такой случай пропустила бы.
    """
    offenders = []
    for path in _js_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for field in SECRET_FIELDS:
                if re.search(rf"(^|[{{,\s]){field}\s*:", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "секретное поле пишется в тело запроса напрямую — пустое поле формы "
        "уйдёт как «сотри» и сотрёт сохранённый секрет:\n" + "\n".join(offenders)
    )


def test_secret_inputs_are_only_read_through_the_helper():
    """`input.value` секретного поля не читается в обход `withSecret`."""
    offenders = []
    for path in _js_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in SECRET_INPUTS:
                if re.search(rf"\b{name}\.value\b", line) and "= ''" not in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, "значение секретного поля читается напрямую:\n" + "\n".join(
        offenders
    )


def test_helper_module_exists_and_sends_only_what_the_user_typed():
    """Поведение самой `withSecret` — на настоящем движке JS.

    Статическая проверка выше говорит лишь, что все ходят через помощник;
    что именно он делает — вопрос отдельный, и отвечать на него
    рассуждением о коде нельзя.

    Сценариев четыре, и три из них — «не отправлять». Самый коварный —
    поле с МАСКОЙ (задача 9.14: поля теперь заполнены, чтобы владелец
    видел, что ключ на месте). Маска в поле выглядит как значение; уйди
    она на сервер — ключ был бы заменён строкой вида `sk-or6...cdef`,
    и подмена обнаружилась бы только отказом OpenRouter.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("нет node — поведение withSecret не проверено")
    assert SECRETS_JS.exists(), "нет модуля static/js/secrets.js"

    probe = f"""
    import {{ withSecret }} from '{SECRETS_JS.as_posix()}';
    const field = (value, data) => ({{ value, dataset: data }});
    const send = (input) => withSecret({{ model: 'x' }}, 'api_key', input);
    console.log(JSON.stringify({{
      masked:    send(field('sk-or6...cdef', {{ masked: '1' }})),
      untouched: send(field('sk-or-v1-abc', {{}})),
      emptied:   send(field('   ', {{ touched: '1' }})),
      typed:     send(field(' sk-or-v1-abc ', {{ touched: '1' }})),
    }}));
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert "api_key" not in out["masked"], (
        "маска ушла на сервер — она заменила бы собой настоящий ключ"
    )
    assert "api_key" not in out["untouched"], (
        "нетронутое поле ушло в тело — сервер получит команду о значении, "
        "которого пользователь не вводил"
    )
    assert "api_key" not in out["emptied"], (
        'пустое поле ушло как "" — сервер прочтёт это как «сотри»'
    )
    assert out["typed"]["api_key"] == "sk-or-v1-abc", "введённое значение потерялось"


def test_interface_never_claims_a_secret_that_is_not_stored():
    """Плейсхолдер секретного поля выставляется в обеих ветках.

    Прежний код писал маску только при `has_key`, а обратно не сбрасывал:
    после стирания ключа поле продолжало показывать «Ключ: sk-or…abcd» —
    интерфейс утверждал ровно то, чего в базе не было.
    """
    offenders = []
    for path in _js_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in SECRET_INPUTS:
                if re.search(rf"\b{name}\.placeholder\s*=", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "плейсхолдер секретного поля выставляется вручную — состояние "
        "рисуется одной функцией fillSecretField, у которой есть обе "
        "ветки:\n" + "\n".join(offenders)
    )


def test_clearing_a_secret_is_a_separate_deliberate_action():
    """Очистка не исчезла вместе с дырой: она стала явной кнопкой."""
    assert SECRETS_JS.exists(), "нет модуля static/js/secrets.js"
    source = SECRETS_JS.read_text(encoding="utf-8")
    assert "clearSecret" in source, (
        "нет способа осознанно очистить секрет — контракт С16 («пустая "
        "строка = очистить») перестал быть достижимым из интерфейса"
    )
    markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for button in ("clearApiKeyBtn", "clearTgBotTokenBtn", "clearWebhookBtn"):
        assert button in markup, f"в разметке нет кнопки очистки {button}"
