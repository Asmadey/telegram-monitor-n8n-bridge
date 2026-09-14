"""Человек должен знать, чем рискует (задача 13.1).

Открытый вопрос №3 плана, не закрытый с 2026-09-05. Подключая аккаунт, человек
отдаёт сервису **доступ на чтение всей своей переписки** — это следует из
самого MTProto, а не из чьей-то щедрости. Второе: массовая автоматизация
пользовательских аккаунтов — серая зона ToS Telegram, и аккаунт могут
ограничить. В модалке об этом не было ни слова: api_id, api_hash, телефон,
кнопка.

Цена ошибки здесь не техническая, и починить её задним числом нельзя. Человек
узнаёт об объёме доступа в день, когда что-то случится, — а MTProto-сессию,
в отличие от пароля, нельзя отозвать удалённо с той стороны.

Два правила:

1. **Сказать до того, как принять номер.** Предупреждение стоит в шаге с
   телефоном и выше поля: предупреждение под кнопкой читают после нажатия.
2. **Согласие — действие, а не умолчание.** Галочка снята, кнопка
   заблокирована. Заранее проставленная галочка — не согласие, а оформление
   согласия.

Проверки статические — разбор разметки и модуля. Живой клик они не заменяют,
но ловят ровно то, что ломалось в этом проекте: поле, которого нет; обработчик,
который не исполняется под CSP; текст, который уехал не туда.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
AUTH_JS = (ROOT / "static" / "js" / "auth.js").read_text(encoding="utf-8")

NOTICE_ID = "tgRiskNotice"
CONSENT_ID = "tgRiskAccepted"


def _step_phone() -> str:
    start = INDEX.index('id="stepPhone"')
    end = INDEX.index('id="stepCode"', start)
    return INDEX[start:end]


def test_the_notice_lives_in_the_phone_step_above_the_field():
    """Предупреждение под кнопкой читают после нажатия."""
    step = _step_phone()
    assert f'id="{NOTICE_ID}"' in step, "в шаге с телефоном нет предупреждения о рисках"
    assert step.index(f'id="{NOTICE_ID}"') < step.index('id="authPhone"'), (
        "предупреждение стоит ниже поля телефона — его прочитают уже после ввода"
    )


def test_the_notice_names_both_risks():
    """Объём доступа и риск для аккаунта — разные вещи, нужны обе."""
    step = _step_phone().lower()
    assert "перепис" in step, (
        "не сказано главное: сервис получает доступ на чтение переписки"
    )
    assert "ограничить" in step or "ограничен" in step, (
        "не сказано, что аккаунт могут ограничить: это риск для самого человека"
    )


def test_consent_is_an_action_not_a_default():
    step = _step_phone()
    match = re.search(rf'<input[^>]*id="{CONSENT_ID}"[^>]*>', step)
    assert match, "нет отдельного согласия — предупреждение можно пролистать"
    tag = match.group(0)
    assert 'type="checkbox"' in tag, f"согласие не отмечается: {tag}"
    assert "checked" not in tag, (
        "галочка проставлена заранее — это оформление согласия, а не согласие"
    )


def test_the_button_is_locked_until_consent():
    step = _step_phone()
    button = re.search(r'<button[^>]*id="sendCodeBtn"[^>]*>', step)
    assert button, "кнопка отправки кода не найдена в шаге с телефоном"
    assert "disabled" in button.group(0), (
        "кнопка активна без согласия — блокировка только в JS означает, что при "
        "медленной загрузке модуля нажать можно раньше"
    )


def test_the_module_unlocks_the_button_on_consent():
    """И блокирует обратно: снятая галочка обязана закрывать кнопку."""
    assert CONSENT_ID in AUTH_JS, "модуль не знает про согласие"
    assert re.search(rf"{CONSENT_ID}[\s\S]{{0,400}}addEventListener", AUTH_JS), (
        "на согласие не повешен обработчик — галочка ничего не меняет"
    )
    assert re.search(r"sendCodeBtn\.disabled\s*=\s*!", AUTH_JS), (
        "состояние кнопки не выводится из согласия"
    )


def test_no_inline_handler_in_the_notice():
    """CSP `script-src 'self'`: инлайновый обработчик молча не исполнится."""
    assert not re.search(r"\son(click|change|input)=", _step_phone()), (
        "инлайновый обработчик в шаге подключения — под CSP он мёртв"
    )
