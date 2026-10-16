"""Отказ почты не должен ни молчать, ни выдавать аккаунт (задача 13.15).

Владелец спросил 24 сентября: «почему не работает отправка письма? Я же
подключал рассылщик по API». Ключ Resend на проде задан и нужного формата.
Не задан `MAIL_FROM` — а значит, работает умолчание
`Teleton <onboarding@resend.dev>`, тестовый отправитель Resend. Документация
Resend говорит прямо: с ним письма уходят ТОЛЬКО на адрес владельца
аккаунта Resend, любому другому получателю — 403:

    You can only send testing emails to your own email address (…).
    To send emails to other recipients, please verify a domain …

Это настройка, её чинит владелец. Но разбор вскрыл два дефекта в коде.

**1. Молчание.** Ни принятое письмо, ни отказ не оставляли в журнале ни
строки. Запрос сброса 22 сентября вернул 200 за 226 мс — и по логам нельзя
было сказать, ушло письмо, отклонено или адреса просто нет в базе. Причину
пришлось восстанавливать по косвенным уликам: флагу `Secure` у живой cookie
(значит, режим production) и имени переменной, которой нет.

**2. Перечислитель, от которого эндпоинт защищался.** Сам эндпоинт отвечает
одинаково для существующего и несуществующего адреса — ровно чтобы по ответу
нельзя было перебирать пользователей (`test_24`). Но отказ Resend поднимался
наверх как 500, а случается он только для СУЩЕСТВУЮЩЕГО адреса: для
несуществующего письмо не отправляется вовсе. Пока почта сломана — а с
тестовым отправителем она сломана для всех, кроме одного, — 500 и 200 делят
адреса на зарегистрированные и нет. `test_24` этого не видел: он работает в
режиме разработки, где письмо пишется в файл и отказать не может.

Контракт `test_28` сохраняется: транспорт по-прежнему падает громко. Меняется
только то, КОМУ громко: оператору в журнал, а не запросившему в ответ.
"""

import logging
import re

import httpx
import pytest

from app.config import get_settings
from app.models import User
from app.security.passwords import hash_password
from app.services import mailer

KNOWN = "known@example.com"
UNKNOWN = "nobody@example.com"
TESTING_REFUSAL = (
    "You can only send testing emails to your own email address "
    "(owner@example.com). To send emails to other recipients, please verify "
    "a domain at resend.com/domains, and change the `from` address to an "
    "email using this domain."
)


@pytest.fixture
def resend(monkeypatch):
    """Почтовик в боевом режиме, а приложение — нет.

    Весь сервис в production перевёл бы cookie в `Secure`, и тестовый клиент
    по http перестал бы их отправлять — отказ был бы про CSRF, а не про
    почту. Поэтому боевой режим включается только у почтовика.
    """
    base = get_settings()
    prod = base.model_copy(
        update={
            "environment": "production",
            "resend_api_key": "re_test_not_a_real_key",
            "mail_from": "Teleton <onboarding@resend.dev>",
            "app_base_url": "https://teleton.example.com",
        }
    )
    monkeypatch.setattr(mailer, "get_settings", lambda: prod)

    state: dict = {"status": 200, "body": {"id": "em_accepted_1"}, "sent": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["sent"].append(request.read().decode())
        return httpx.Response(state["status"], json=state["body"])

    monkeypatch.setattr(mailer, "RESEND_TRANSPORT", httpx.MockTransport(handler))
    return state


def _refuse(state: dict) -> None:
    state["status"] = 403
    state["body"] = {
        "statusCode": 403,
        "name": "validation_error",
        "message": TESTING_REFUSAL,
    }


async def _known_user(db) -> None:
    db.add(User(email=KNOWN, password_hash=hash_password("long-enough-password")))
    await db.commit()


def _token_from(sent: list[str]) -> str:
    match = re.search(r"token=([A-Za-z0-9._\-]+)", sent[0])
    assert match, "в письме нет ссылки с токеном — проверять нечего"
    return match.group(1)


# --------------------------------------------------------------------------
# Ответ не выдаёт аккаунт
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_letter_does_not_reveal_the_account(anon_client, db, resend):
    """Главное утверждение: отказ почты не делит адреса на «есть» и «нет»."""
    await _known_user(db)
    _refuse(resend)

    known = await anon_client.post("/auth/password-reset", json={"email": KNOWN})
    unknown = await anon_client.post("/auth/password-reset", json={"email": UNKNOWN})

    assert resend["sent"], "письмо даже не попыталось уйти — тест бессмыслен"
    assert known.status_code == unknown.status_code, (
        f"статусы различаются ({known.status_code} против {unknown.status_code}): "
        "пока почта сломана, по ним перебираются зарегистрированные адреса"
    )
    assert known.json() == unknown.json(), "тела ответов различаются"


# --------------------------------------------------------------------------
# Журнал знает, что случилось
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refusal_is_logged_with_its_reason(anon_client, db, resend, caplog):
    """Отказ обязан оставить причину — словами Resend, а не «что-то сломалось»."""
    await _known_user(db)
    _refuse(resend)
    caplog.set_level(logging.INFO)

    await anon_client.post("/auth/password-reset", json={"email": KNOWN})

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("403" in m and "testing emails" in m for m in errors), (
        f"в журнале нет причины отказа: {errors}"
    )


@pytest.mark.asyncio
async def test_a_testing_sender_is_named_as_the_cause(anon_client, db, resend, caplog):
    """Оператору нужен не код, а действие: какой домен и какая переменная."""
    await _known_user(db)
    _refuse(resend)
    caplog.set_level(logging.INFO)

    await anon_client.post("/auth/password-reset", json={"email": KNOWN})

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "resend.dev" in text and "MAIL_FROM" in text, (
        f"журнал не называет тестовый отправитель и переменную: {text[-400:]}"
    )


@pytest.mark.asyncio
async def test_an_accepted_letter_leaves_a_trace(anon_client, db, resend, caplog):
    """Принятое письмо тоже видно: по id его находят в кабинете Resend."""
    await _known_user(db)
    caplog.set_level(logging.INFO)

    await anon_client.post("/auth/password-reset", json={"email": KNOWN})

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "em_accepted_1" in text, f"принятое письмо не оставило следа: {text[-400:]}"


# --------------------------------------------------------------------------
# Что в журнал не попадает
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_the_token_and_the_address_stay_out_of_the_log(
    anon_client, db, resend, caplog, refused
):
    """Антивакуум громкости: журнал говорит о письме, но не пересказывает его.

    Токен в журнале — это вход в чужой аккаунт для любого, кто читает логи.
    Адрес — персональные данные; для поиска письма хватает id и маски.
    """
    await _known_user(db)
    if refused:
        _refuse(resend)
    caplog.set_level(logging.DEBUG)

    await anon_client.post("/auth/password-reset", json={"email": KNOWN})

    token = _token_from(resend["sent"])
    # Только журнал приложения. Первая версия теста смотрела все записи и
    # нашла адрес — в отладочном выводе aiosqlite, который на уровне DEBUG
    # печатает параметры SQL. Это не наш журнал и не боевой уровень; зато
    # на нём тест проверял бы драйвер базы, а не то, что пишем мы.
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert token not in text, "токен сброса попал в журнал"
    assert KNOWN not in text, "адрес получателя в журнале целиком"
