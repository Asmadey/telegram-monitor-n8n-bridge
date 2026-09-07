# 🚀 Teleton — мониторинг Telegram-каналов через MTProto

> Мульти-тенант SaaS на **FastAPI + Telethon (MTProto) + PostgreSQL**: читает
> Telegram-каналы от лица живого аккаунта пользователя, отсеивает дубликаты,
> анализирует посты через LLM и доставляет их в n8n и Telegram-бота.

Каждый пользователь регистрируется, подключает **свой** Telegram-аккаунт и
работает только со своими данными. Развёртывание — Railway (API + воркер) и
Vercel (фронтенд); подробности — [docs/DEPLOY.md](docs/DEPLOY.md).

---

## ⚠️ Чем этот проект опасен (прочитать до запуска)

На сервере оседают **MTProto-сессии чужих Telegram-аккаунтов**. Такая сессия —
не пароль: её нельзя сбросить удалённо, и она даёт полное чтение переписки
владельца. Утечка базы — это не «утечка данных сервиса», а компрометация всех
подключённых аккаунтов разом.

Отсюда всё остальное: сессии лежат в БД зашифрованными (Fernet,
`APP_ENCRYPTION_KEY` только в переменных окружения), приложение закрыто по
умолчанию (публичные маршруты перечисляются явно), на чужие ресурсы всегда
404, а не 403. Правила работы — [AGENTS.md](AGENTS.md).

**`APP_ENCRYPTION_KEY` нельзя менять после первого запуска.** Смена без
перешифровки делает нечитаемыми все сессии, то есть выбрасывает всех
пользователей из их аккаунтов.

---

## 🌟 Что умеет

1. **Учётные записи и доступ.** Регистрация, вход, сброс пароля, вход через
   Google (Firebase — провайдер идентичности, сессия своя), админка. Сессии — в
   БД (отзываются удалением строки), cookie подписанная и `HttpOnly`, CSRF —
   double-submit, ограничение частоты на входе и на запросе кода Telegram.
2. **Подключение Telegram без консоли.** Телефон → код → пароль 2FA прямо в
   браузере. Сессия сохраняется как `StringSession` в БД зашифрованной —
   файлов `.session` не создаётся (на Railway файловая система эфемерна).
3. **Многоканальный мониторинг.** Источник по `@username`, ссылке или числовому
   ID; свой интервал и лимит постов на канал; выбор из собственных диалогов.
4. **Дедупликация без гонок.** Один запрос `INSERT ... ON CONFLICT DO NOTHING
   RETURNING`: ручной запуск и тик планировщика по одному каналу не могут
   отправить один пост дважды.
5. **Анализ через LLM.** OpenRouter с потолком символов на запрос, месячным
   счётчиком токенов и автоотключением при превышении.
6. **Доставка.** Telegram-бот и n8n-вебхук. Адрес вебхука проверяется по
   **резолвнутому IP** (защита от SSRF во внутреннюю сеть), редиректы не
   следуются.
7. **Лента и журнал.** История выполненных заданий с аватарками каналов,
   журнал событий с затиранием секретов в текстах ошибок, автоочистка старше
   7/14/30/60/90 дней.

---

## 🏗️ Архитектура

Два процесса из **одного** образа. Это не оптимизация, а необходимость:
долгоживущими Telethon-клиентами владеет только воркер — два процесса на одном
auth-key дают `AUTH_KEY_DUPLICATED`, и Telegram может убить сессию пользователя.

| Процесс | Команда | Telethon |
|---|---|---|
| `web` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | короткоживущий клиент только на время авторизации |
| `worker` | `python -m app.worker` | единственный владелец пула долгоживущих клиентов |

```mermaid
flowchart TD
    subgraph Telegram ["Telegram Cloud (MTProto)"]
        TG["📡 Каналы и группы"]
    end

    subgraph Railway ["Railway"]
        API["⚡ web — FastAPI: auth, API, вход в Telegram"]
        W["⏰ worker — очередь, расписание, доставка"]
        DB[("🗄️ PostgreSQL: users, sessions, telegram_accounts,\nmonitors, sent_messages, feed_items, logs,\nintegrations, jobs, chat_avatars")]
    end

    subgraph Vercel ["Vercel"]
        UI["📰 Лента | 📡 Каналы | 🔗 Интеграции | 📋 Журнал"]
    end

    N8N["🌐 n8n / Telegram-бот"]

    TG <--> W
    TG <--> API
    API <--> DB
    W <--> DB
    UI -->|"/api/* (rewrite)"| API
    W -->|POST JSON| N8N
```

**Тик воркера** (каждые 30 секунд): вернуть в очередь задачи, брошенные
умершим процессом → разобрать очередь `jobs` → опросить мониторы с истёкшим
интервалом → суточная автоочистка. Опрос канала: выборка → дедупликация →
LLM → бот → n8n → запись в ленту.

Подробная карта модулей и API — [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md),
план работ и статус — [docs/PLAN.md](docs/PLAN.md).

---

## 📦 Формат вебхука в n8n

```json
{
  "source": "telethon_monitor",
  "event": "telegram_messages_batch",
  "timestamp": "2026-09-05T15:20:00Z",
  "chat_id": -1001143063102,
  "chat_title": "Finder.work: работа и вакансии",
  "chat_username": "theyseeku",
  "messages_count": 1,
  "ai_analysis": "Короткая выжимка от LLM, если анализ включён",
  "messages": [
    {
      "id": 38115,
      "date": "2026-09-05T12:00:00+00:00",
      "sender": "Finder.work",
      "sender_id": -1001143063102,
      "is_outgoing": false,
      "text": "Ищем Python/AI разработчика...",
      "has_media": false,
      "views": 320,
      "forwards": 2,
      "reactions_count": 14,
      "reactions": [
        { "emoji": "🔥", "count": 10 },
        { "emoji": "👍", "count": 4 }
      ],
      "post_url": "https://t.me/theyseeku/38115"
    }
  ]
}
```

---

## 🚀 Локальный запуск

### 1. Окружение

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

### 2. Переменные окружения

`.env` в репозиторий не попадает и приложением **не пишется** — конфигурация
читается только из окружения (`app/config.py`).

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/teleton
APP_ENCRYPTION_KEY=<Fernet.generate_key().decode(), 44 символа>
SECRET_KEY=<случайная строка для подписи cookie>
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
APP_BASE_URL=http://127.0.0.1:8000
ENVIRONMENT=development
```

Ключ шифрования генерируется так:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Схема БД

Схема меняется **только** миграцией Alembic; `ALTER TABLE` в коде приложения
запрещён (правило держит `tests/test_13_migrations.py`).

```bash
alembic upgrade head
```

### 4. Запуск

Два процесса в двух терминалах:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
python -m app.worker
```

Интерфейс — <http://127.0.0.1:8000>, Swagger — <http://127.0.0.1:8000/docs>.

### 5. Перенос данных из старой SQLite (разово)

```bash
python -m scripts.migrate_sqlite_to_pg --user-email owner@example.com
```

Скрипт идемпотентен и шифрует секреты при переносе; без `APP_ENCRYPTION_KEY`
он отказывается работать. Историю `sent_messages` переносить обязательно —
иначе первый же опрос отправит в n8n все старые посты как новые.

---

## ✅ Проверки

```bash
pytest -q
ruff check . && ruff format --check .
mypy app/
bandit -r app/ -ll && pip-audit
```

Те же пять джобов гоняет CI (`.github/workflows/ci.yml`), включая секрет-скан
по всему дереву и истории. Поведенческие тесты без живого Postgres пишут
`pytest.skip` — тест, зеленеющий в отсутствие базы, хуже отсутствующего.

---

## 🚢 Деплой

Railway — два сервиса (`web` и `worker`) из одного образа, общий managed
Postgres; Vercel — фронтенд с переписыванием `/api/*` на Railway (так cookie
остаются первой стороной и работают во всех браузерах). Пошагово —
[docs/DEPLOY.md](docs/DEPLOY.md).

---

## 🛠️ Стек

* **Backend:** Python 3.11, FastAPI, Telethon, SQLAlchemy 2.0 (async), Alembic,
  PostgreSQL, httpx, pydantic-settings, passlib/bcrypt, cryptography.
* **Frontend:** ванильный SPA на ES-модулях, без сборки.
* **Интеграции:** OpenRouter (LLM), n8n webhook, Telegram Bot API, Firebase
  (только верификация ID-токенов).

---

## 📄 Лицензия

MIT License.
