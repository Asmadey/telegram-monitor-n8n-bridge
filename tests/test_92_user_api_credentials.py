"""Ключи MTProto принадлежат пользователю, а не сервису.

Открытый вопрос №1 плана (раздел 13) решён владельцем 2026-09-07 в пользу
персональных ключей: пользователь регистрирует кабинет, вносит в него свои
`api_id`/`api_hash` с my.telegram.org, и подключение его Telegram-аккаунта
идёт ТОЛЬКО через них.

Почему это не косметика. Общий `api_id` на весь сервис означает, что сотни
чужих аккаунтов логинятся через одно приложение: Telegram ограничивает
такие `api_id`, и тогда перестаёт работать вход сразу у всех. Персональные
ключи размазывают риск по владельцам аккаунтов — каждый отвечает за своё.

Отсюда же главное требование теста: подстановки из ENV быть не должно.
Молчаливый откат на ключ сервиса выглядел бы как «всё работает» ровно до
того дня, когда сервисный ключ ограничат.

`api_hash` — секрет: он шифруется в базе и не отдаётся наружу никогда,
как ключ OpenRouter и токен бота (0.3, 3.4).
"""

import pathlib

import pytest
from conftest import act_as
from sqlalchemy import select

from app.models import TelegramCredential
from app.security.crypto import decrypt

# Фиктивные значения собираются в рантайме: секрет-скан не отличает
# выдуманный хеш от настоящего и справедливо ловит литерал (урок test_53).
FAKE_API_HASH = "0" * 8 + "f" * 8 + "0" * 8 + "f" * 8
OTHER_API_HASH = "a" * 32
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_user_saves_own_keys_and_gets_them_back_masked(anon_client, db, user):
    await act_as(anon_client, db, user)

    saved = await anon_client.post(
        "/api/telegram/credentials",
        json={"api_id": 1234567, "api_hash": FAKE_API_HASH},
    )
    assert saved.status_code == 200, saved.text
    assert FAKE_API_HASH not in saved.text, "api_hash вернулся вызывающему"

    status = (await anon_client.get("/api/telegram/status")).json()
    assert status["api_id"] == 1234567
    assert status["has_api_hash"] is True
    assert FAKE_API_HASH not in str(status), "api_hash уехал в статус"


@pytest.mark.asyncio
async def test_api_hash_is_encrypted_at_rest(anon_client, db, user):
    await act_as(anon_client, db, user)
    await anon_client.post(
        "/api/telegram/credentials",
        json={"api_id": 1234567, "api_hash": FAKE_API_HASH},
    )

    row = (await db.scalars(select(TelegramCredential))).first()
    assert row is not None, "ключи не сохранились"
    assert FAKE_API_HASH not in (row.api_hash_encrypted or ""), (
        "api_hash лежит в базе открытым текстом"
    )
    assert decrypt(row.api_hash_encrypted) == FAKE_API_HASH


@pytest.mark.asyncio
async def test_saving_without_hash_keeps_the_stored_one(anon_client, db, user):
    """Правило 0.3: поле, которого нет в запросе, не затирает секрет.

    Иначе сохранение формы (пользователь поправил только api_id) вытирает
    хеш, и подключение ломается без единого сообщения об ошибке.
    """
    await act_as(anon_client, db, user)
    await anon_client.post(
        "/api/telegram/credentials",
        json={"api_id": 1234567, "api_hash": FAKE_API_HASH},
    )

    again = await anon_client.post(
        "/api/telegram/credentials", json={"api_id": 7654321}
    )
    assert again.status_code == 200, again.text

    status = (await anon_client.get("/api/telegram/status")).json()
    assert status["api_id"] == 7654321
    assert status["has_api_hash"] is True, "хеш затёрт сохранением одного api_id"


@pytest.mark.asyncio
async def test_keys_never_cross_between_tenants(
    anon_client, second_client, db, user_a, user_b
):
    await act_as(anon_client, db, user_a)
    await act_as(second_client, db, user_b)
    await anon_client.post(
        "/api/telegram/credentials",
        json={"api_id": 1234567, "api_hash": FAKE_API_HASH},
    )

    foreign = (await second_client.get("/api/telegram/status")).json()
    assert foreign["api_id"] is None, "чужой api_id виден второму тенанту"
    assert foreign["has_api_hash"] is False
    assert FAKE_API_HASH not in str(foreign)


@pytest.mark.asyncio
async def test_login_without_own_keys_is_refused_not_silently_shared(
    anon_client, db, user
):
    """Без своих ключей вход не начинается — и не подменяется ключом сервиса."""
    await act_as(anon_client, db, user)

    answer = await anon_client.post(
        "/api/telegram/send-code", json={"phone": "+79990000000"}
    )
    assert answer.status_code == 400, answer.text
    detail = answer.json().get("detail", "")
    assert "api" in detail.lower() or "ключ" in detail.lower(), (
        f"отказ не объясняет, чего не хватает: {detail!r}"
    )


def test_application_never_falls_back_to_service_wide_keys():
    """Свип: приложение не читает ключи MTProto из ENV.

    Молчаливый откат на общий ключ — самый вероятный способ «починить»
    отказ выше, и он же незаметно возвращает решение, которое владелец
    отменил. Скрипт переноса (`scripts/`) и сама модель настроек под
    правило не попадают: там ключи задаёт оператор своей рукой.
    """
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "config.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "telegram_api_id" in line or "telegram_api_hash" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "приложение всё ещё берёт ключи MTProto из окружения:\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_pool_builds_the_client_with_the_owners_keys(db, user):
    """Воркер поднимает клиент теми же ключами, что ввёл владелец."""
    from app.services.tg_credentials import save_credentials
    from app.services.tg_pool import TelegramClientPool

    await save_credentials(db, user.id, api_id=424242, api_hash=OTHER_API_HASH)
    await db.commit()

    seen = {}

    class FakeClient:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    def factory(user_id, session_string, *, api_id, api_hash):
        seen.update(user_id=user_id, api_id=api_id, api_hash=api_hash)
        return FakeClient()

    pool = TelegramClientPool(client_factory=factory)
    await pool.get(user.id, "session-string", api_id=424242, api_hash=OTHER_API_HASH)

    assert seen == {
        "user_id": user.id,
        "api_id": 424242,
        "api_hash": OTHER_API_HASH,
    }


def test_interface_lets_the_owner_enter_the_keys():
    """Трипваер интерфейса: эндпоинт без экрана — недоступная функция.

    Написан ПОСЛЕ правки разметки, а не до неё (обычный порядок CDD здесь
    неприменим: предмет проверки — верстка, у которой нет промежуточного
    состояния). Он защищает не сегодняшнюю правку, а завтрашнюю: ровно так
    в сентябре обнаружилось, что вход через Google работал на сервере и не
    имел ни одной кнопки.
    """
    import re

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def tag(element_id: str) -> str:
        # Атрибуты идут в любом порядке, поэтому берётся тег целиком, а не
        # хвост после id: первая версия теста срезала как раз ту половину,
        # где стоял type.
        found = re.search(rf"<input[^>]*id=\"{element_id}\"[^>]*>", html)
        assert found, f"поля {element_id} нет в разметке"
        return found.group(0)

    assert 'id="saveApiKeysBtn"' in html, "ключи некуда сохранить — нет кнопки"
    assert "readonly" not in tag("settingsApiId"), (
        "поле API ID осталось только для чтения — ввести свой ключ нельзя"
    )
    assert 'type="password"' in tag("settingsApiHash"), (
        "api_hash вводится открытым текстом — это секрет, а не логин"
    )

    script = (ROOT / "static" / "js" / "auth.js").read_text(encoding="utf-8")
    assert "/api/telegram/credentials" in script, "кнопка ни к чему не подключена"
    assert "settingsApiHash.value = ''" in script, (
        "поле хеша заполняется с сервера — сырой секрет не должен возвращаться"
    )
