"""Отказ входа через Google обязан называть причину (задача 13.14).

Найдено владельцем 24 сентября: после переезда на новый адрес
`asmadey.vercel.app` вход через Google перестал работать, и на экране было
одно и то же «Не удалось войти через Google» — без единого намёка почему.

**Причина снаружи кода, и она проверена, а не угадана.** Вход идёт через
Firebase `signInWithPopup`, а у Firebase свой список разрешённых доменов
(Authentication → Settings → Authorized domains). Тот же публичный вызов,
которым SDK сам сверяет домен (`identitytoolkit … /v1/projects`), вернул:

    localhost, mtproto-ai.firebaseapp.com, mtproto-ai.web.app,
    telegram-monitor-n8n-bridge.vercel.app

Старый адрес в списке есть, нового нет. SDK отвечает кодом
`auth/unauthorized-domain` — ещё до того, как открыть окно Google.

**Дефект — внутри кода:** этот код отказа никто не видел. `catch` отличал
только закрытое пользователем окно, всё остальное превращалось в одну и ту
же фразу, а в консоль не писалось ничего. Неразрешённый домен, заблокированное
всплывающее окно и обрыв сети выглядели одинаково — и найти причину можно
было только чтением исходников Firebase.

Правило: код причины уходит в консоль ВСЕГДА, а две причины, которые чинит
сам человек или оператор, получают свои слова.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static" / "js" / "auth-pages.js").read_text(encoding="utf-8")


def _sign_in_body() -> str:
    """Тело `signInWithGoogle` — проверяем его, а не весь файл.

    Иначе совпадение нашлось бы в чужом месте, например в комментарии.
    """
    start = SOURCE.index("async function signInWithGoogle")
    end = SOURCE.index("\n  }\n", start)
    body = SOURCE[start:end]
    # комментарии не код: строка «auth/unauthorized-domain» в пояснении не
    # должна зеленить тест, который проверяет обработку
    body = re.sub(r"//[^\n]*", "", body)
    return body


def test_an_unauthorized_domain_gets_its_own_words():
    """Главный случай владельца: адрес не внесён в список Firebase."""
    body = _sign_in_body()
    assert "'auth/unauthorized-domain'" in body, (
        "неразрешённый домен неотличим от любого другого отказа"
    )


def test_the_message_names_the_address():
    """Оператору нужен адрес, который добавить, — человеку тоже полезно его видеть."""
    body = _sign_in_body()
    assert "location.hostname" in body, (
        "сообщение не называет адрес — непонятно, какой домен добавлять"
    )


def test_a_blocked_popup_gets_its_own_words():
    """Вторая причина, которую чинит сам человек: браузер запретил окно."""
    assert "'auth/popup-blocked'" in _sign_in_body(), (
        "заблокированное окно выглядит как поломка сервиса"
    )


def test_the_cause_always_reaches_the_console():
    """Следующая незнакомая причина не должна снова стать немой."""
    assert "console.error" in _sign_in_body(), (
        "код отказа никуда не пишется — причину снова придётся угадывать"
    )


def test_a_closed_window_stays_silent():
    """Антивакуум: закрытое окно — решение человека, а не ошибка."""
    body = _sign_in_body()
    assert "'auth/popup-closed-by-user'" in body, "закрытое окно стало ошибкой"
    # сначала наличие, потом порядок: без записи в консоль сравнивать нечего,
    # и тест обязан упасть на утверждении, а не на ValueError из index()
    assert "console.error" in body, "код отказа никуда не пишется"
    closed = body.index("'auth/popup-closed-by-user'")
    logged = body.index("console.error")
    assert closed < logged, "закрытое пользователем окно пишется в консоль как отказ"
