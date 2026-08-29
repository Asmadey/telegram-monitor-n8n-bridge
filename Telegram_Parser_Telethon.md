# Исследование: Telegram Parser на базе Telethon (Kimi Swarm Result)

# Техническое руководство по созданию парсера сообщений на базе Telethon

## Содержание
1. [Telegram API: UserBot vs Bot API](#1-telegram-api-userbot-vs-bot-api)
2. [Получение api_id и api_hash](#2-получение-api_id-и-api_hash)
3. [Программное вступление в закрытые группы](#3-программное-вступление-в-закрытые-группы)
4. [Асинхронное чтение сообщений](#4-асинхронное-чтение-сообщений)
5. [Риски блокировки и методы защиты](#5-риски-блокировки-и-методы-защиты)
6. [Архитектура приложения](#6-архитектура-приложения)

---

## 1. Telegram API: UserBot vs Bot API

### Принципиальные различия

| Параметр | Bot API | User API (MTProto) |
|----------|---------|-------------------|
| **Идентификация** | Токен `bot123456:ABC-DEF...` | `api_id` + `api_hash` + номер телефона |
| **Доступ к истории** | Только с момента добавления | Полная история чата |
| **Закрытые группы** | Только если бот добавлен админом | Доступ через инвайт-ссылки |
| **Лимиты** | 30 сообщений/сек | Гибкие, но строгие flood-лимиты |
| **User-Agent** | Официальный ограниченный API | Полный доступ к функциям клиента |

### Почему именно Telethon (UserBot)?

**Ключевое ограничение Bot API:** Боты не могут читать сообщения в группах/каналах, где они не состоят, и не имеют доступа к истории до момента их добавления.

**MTProto через Telethon позволяет:**
- Вступать в группы по инвайт-ссылкам программно
- Читать историю переписки retrospectively
- Получать метаданные пользователей (bio, фото, последний вход)
- Обходить ограничения Bot API на скачивание медиа > 20MB

```python
# Критическое различие в доступе
from telethon import TelegramClient

# UserBot может просматривать любые чаты, где состоит аккаунт
async with TelegramClient('user_session', api_id, api_hash) as client:
    async for message in client.iter_messages(private_channel_id, limit=1000):
        print(message.text)  # Доступ к полной истории
```

---

## 2. Получение api_id и api_hash

### Пошаговая инструкция

**Шаг 1: Регистрация приложения**
1. Перейдите на [my.telegram.org](https://my.telegram.org)
2. Авторизуйтесь с помощью номера телефона (придет код в Telegram)
3. Выберите раздел **"API development tools"**

**Шаг 2: Создание приложения**
- **App title:** Произвольное название (например, `MessageParser`)
- **Short name:** Уникальный идентификатор без пробелов (`msg_parser_2024`)
- **URL:** Можно оставить пустым или указать `https://localhost`
- **Platform:** Desktop
- **Description:** "Message parsing automation tool"

**Шаг 3: Сохранение credentials**
После создания система выдаст:
```yaml
api_id: 12345678          # Числовой ID
api_hash: "1a2b3c4d..."   # 32-символьная строка
```

### Безопасность credentials
```python
# НИКОГДА не храните в коде!
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE = os.getenv('TELEGRAM_PHONE')
```

---

## 2.5. Работа с сессиями (Личный аккаунт)

### Как авторизовать личный аккаунт
В отличие от бота, личный аккаунт требует ввода кода из SMS/Telegram и (если включено) 2FA пароля. 

**Процесс:**
1. Вы запускаете скрипт `create_user_session.py`.
2. Telethon запрашивает код.
3. После ввода создается файл `personal_account.session` (это SQLite база данных).
4. **ВАЖНО:** Этот файл содержит ваши ключи доступа. Не передавайте его третьим лицам.

### Пример использования сессии в коде:
```python
from telethon import TelegramClient

# Telethon сам подхватит файл 'personal_account.session'
client = TelegramClient('personal_account', api_id, api_hash)

async def main():
    await client.connect()
    # Если файл сессии есть, повторный вход по SMS не потребуется
    if not await client.is_user_authorized():
        # Только если сессия просрочена/удалена
        await client.send_code_request(phone)
        await client.sign_in(phone, input('Код: '))
```

### Метод 1: По инвайт-ссылке

```python
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon import errors

async def join_by_invite(client, invite_link: str):
    """
    Поддерживает форматы:
    - https://t.me/+HASH
    - https://t.me/joinchat/HASH
    - +HASH (прямой хэш)
    """
    # Извлечение хэша из ссылки
    if '/' in invite_link:
        hash_part = invite_link.split('/')[-1].replace('+', '')
    else:
        hash_part = invite_link.replace('+', '')
    
    try:
        updates = await client(ImportChatInviteRequest(hash_part))
        return updates.chats[0]  # Возвращает объект Chat/Channel
    except errors.InviteHashExpiredError:
        print("Ссылка устарела")
    except errors.InviteHashInvalidError:
        print("Невалидная ссылка")
    except errors.UserAlreadyParticipantError:
        print("Уже состоим в группе")
```

### Метод 2: Принятие приглашения от пользователя

```python
from telethon.tl.functions.messages import CheckChatInviteRequest

async def accept_invitation_dialog(client, invite_hash: str):
    """Проверка инвайта перед вступлением"""
    try:
        invite = await client(CheckChatInviteRequest(invite_hash))
        print(f"Канал: {invite.title}, Участников: {invite.participants_count}")
        
        # Принятие решения на основе метаданных
        if invite.participants_count > 1000:
            return await client(ImportChatInviteRequest(invite_hash))
    except Exception as e:
        print(f"Ошибка: {e}")
```

### Метод 3: Поиск публичных каналов по юзернейму

```python
from telethon.tl.functions.channels import JoinChannelRequest

async def join_public_channel(client, username: str):
    """username: @channelname или channelname"""
    username = username.replace('@', '')
    channel = await client.get_entity(username)
    await client(JoinChannelRequest(channel))
    return channel
```

---

## 4. Асинхронное чтение сообщений

### Архитектура обработчиков событий

```python
from telethon import TelegramClient, events
import asyncio

class MessageParser:
    def __init__(self, session_name, api_id, api_hash):
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.client.on(events.NewMessage)
        async def handle_new_message(event):
            """Обработка новых сообщений в реальном времени"""
            if event.is_private:
                await self.process_private_message(event)
            elif event.is_group or event.is_channel:
                await self.process_group_message(event)
        
        @self.client.on(events.NewMessage(pattern=r'(?i).*ключевое слово.*'))
        async def keyword_handler(event):
            """Фильтрация по ключевым словам"""
            await self.save_filtered_message(event.message)
    
    async def process_group_message(self, event):
        message_data = {
            'id': event.id,
            'chat_id': event.chat_id,
            'sender_id': event.sender_id,
            'text': event.text,
            'date': event.date.isoformat(),
            'is_reply': event.is_reply,
            'media': bool(event.media)
        }
        
        # Асинхронная запись в БД/очередь
        await self.persist_message(message_data)
        
        # Обработка медиа
        if event.media:
            await self.download_media_async(event.message)
    
    async def run(self):
        await self.client.start()
        print("Парсер запущен...")
        await self.client.run_until_disconnected()
```

### Историческое сканирование (backfilling)

```python
async def fetch_history(client, entity_id, limit=None, offset_date=None):
    """
    Пагинация по истории сообщений с контролем скорости
    """
    messages = []
    async for message in client.iter_messages(
        entity_id,
        limit=limit,
        offset_date=offset_date,
        reverse=False,  # С новых к старым
        wait_time=1.0   # Задержка между пачками запросов
    ):
        messages.append(message)
        
        # Batch processing
        if len(messages) >= 100:
            await process_batch(messages)
            messages = []
            await asyncio.sleep(2)  # Anti-flood
    
    if messages:
        await process_batch(messages)
```

### Обработка медиа в реальном времени

```python
async def download_media_async(message, download_path='./media/'):
    """Потоковая загрузка с прогрессом"""
    filename = await message.download_media(
        file=download_path,
        progress_callback=lambda current, total: print(
            f'Загружено {current} из {total} байт ({current/total*100:.1f}%)'
        )
    )
    return filename
```

---

## 5. Риски блокировки и методы защиты

### Классификация рисков

| Тип | Триггер | Последствие | Длительность |
|-----|---------|-------------|--------------|
| **FloodWait** | Превышение лимитов запросов | Блокировка метода | 1-86400 сек |
| **Account Ban** | Спам, массовое добавление | Полная блокировка | Перманентно |
| **Phone Ban** | Нарушение ToS | Бан номера | Перманентно |
| **Proxy Detection** | Подозрительные IP | Требование верификации | До подтверждения |

### Стратегия обхода FloodWait

```python
from telethon import errors
import random
import time

class RateLimiter:
    def __init__(self):
        self.last_request_time = 0
        self.min_delay = 2.0  # Минимальная задержка между действиями
        self.jitter = 1.5     # Случайное отклонение
    
    async def execute_with_backoff(self, coro, max_retries=5):
        for attempt in range(max_retries):
            try:
                # Джиттер для маскировки бота
                delay = self.min_delay + random.uniform(0, self.jitter)
                await asyncio.sleep(delay)
                
                return await coro
                
            except errors.FloodWaitError as e:
                wait_time = e.seconds
                print(f"FloodWait: ожидание {wait_time} сек...")
                
                if wait_time > 3600:  # Если больше часа - пропускаем
                    raise Exception("Слишком длительное ожидание")
                
                time.sleep(wait_time + random.randint(5, 15))
        
        raise Exception("Превышено количество попыток")
```

### Использование прокси (SOCKS5/MTProto)

```python
# SOCKS5 прокси
client = TelegramClient(
    'session',
    API_ID,
    API_HASH,
    proxy=('socks5', 'proxy_host', 1080, True, 'username', 'password')
)

# MTProto прокси (рекомендуется для Telegram)
client = TelegramClient(
    'session',
    API_ID,
    API_HASH,
    proxy=('mtproto', 'mtproto_proxy_host', 443, 'secret_key')
)
```

### Анти-спам паттерны

```python
class AntiBanStrategy:
    def __init__(self):
        self.message_counter = 0
        self.session_start = time.time()
    
    def should_pause(self):
        """Адаптивная пауза на основе активности"""
        runtime = time.time() - self.session_start
        rate = self.message_counter / runtime
        
        # Если скорость > 1 msg/sec - принудительная пауза
        if rate > 1.0:
            sleep_time = random.randint(30, 120)
            print(f"Анти-бан пауза: {sleep_time} сек")
            time.sleep(sleep_time)
            self.message_counter = 0
            self.session_start = time.time()
    
    async def human_like_delay(self):
        """Имитация человеческого поведения"""
        # Логнормальное распределение (более реалистично)
        mu, sigma = 2.0, 0.5
        delay = random.lognormvariate(mu, sigma)
        await asyncio.sleep(min(delay, 10.0))
```

### Мониторинг состояния аккаунта

```python
from telethon.tl.functions.users import GetFullUserRequest

async def check_account_health(client):
    """Проверка ограничений на аккаунте"""
    me = await client.get_me()
    full_user = await client(GetFullUserRequest(me))
    
    # Проверка ограничений
    restrictions = full_user.full_user.restrictions
    if restrictions:
        for restriction in restrictions:
            print(f"Ограничение до {restriction.until_date}: {restriction.reason}")
    
    return len(restrictions) == 0
```

---

## 6. Архитектура приложения

### Компонентная схема

```
┌─────────────────┐
│  Load Balancer  │
│   (Proxy Pool)  │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼───┐  ┌──▼────┐
│Worker1│  │Worker2│  ...  WorkerN (Telethon Clients)
└───┬───┘  └──┬────┘
    │         │
    └────┬────┘
         │
┌────────▼────────┐
│  Message Queue  │  (Redis/RabbitMQ)
│  (Buffering)    │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼───┐  ┌──▼────┐
│Storage│  │Media  │
│(Postgre│  │Store  │
│/ClickHouse)│ (S3/MinIO)
└─────────┘  └───────┘
```

### Управление сессиями

```python
import json
from pathlib import Path
from dataclasses import dataclass

@dataclass
class SessionConfig:
    session_name: str
    phone: str
    api_id: int
    api_hash: str
    proxy: dict = None
    active: bool = True

class SessionManager:
    def __init__(self, config_dir='./sessions/'):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(exist_ok=True)
        self.clients = {}
    
    def create_session(self, config: SessionConfig):
        """Создание изолированной сессии"""
        session_path = self.config_dir / config.session_name
        
        client = TelegramClient(
            str(session_path),
            config.api_id,
            config.api_hash,
            proxy=config.proxy
        )
        
        self.clients[config.session_name] = {
            'client': client,
            'config': config,
            'created_at': time.time()
        }
        
        return client
    
    async def rotate_sessions(self):
        """Ротация сессий для распределения нагрузки"""
        active_sessions = [
            name for name, data in self.clients.items() 
            if data['config'].active
        ]
        return random.choice(active_sessions) if active_sessions else None
    
    def export_session_string(self, session_name):
        """Экспорт сессии для переноса (string session)"""
        # Полезно для serverless-развертывания
        client = self.clients[session_name]['client']
        return client.session.save()
```

### Хранение данных

```python
# SQLAlchemy модели для метаданных
from sqlalchemy import Column, Integer, String, DateTime, BigInteger, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Message(Base):
    __tablename__ = 'messages'
    
    id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    sender_id = Column(BigInteger, index=True)
    date = Column(DateTime, index=True)
    text = Column(Text)
    media_type = Column(String(50))  # photo, video, document
    media_path = Column(String(500))  # путь к файлу в S3
    is_forward = Column(Boolean, default=False)
    raw_json = Column(Text)  # Полная сериализация объекта
    
    # Индексы для поиска
    __table_args__ = (
        Index('idx_chat_date', 'chat_id', 'date'),
    )

# Обработка медиа
class MediaProcessor:
    def __init__(self, storage_backend='s3'):
        self.storage = self._init_storage(storage_backend)
    
    async def process_media(self, message, chat_id):
        if not message.media:
            return None
        
        # Генерация уникального имени
        filename = f"{chat_id}/{message.id}_{int(time.time())}"
        
        # Скачивание во временный буфер
        buffer = await message.download_media(bytes)
        
        # Определение MIME-type
        mime_type = message.file.mime_type if message.file else 'application/octet-stream'
        
        # Загрузка в объектное хранилище
        await self.storage.upload(filename, buffer, mime_type)
        
        return {
            'path': filename,
            'size': len(buffer),
            'mime_type': mime_type,
            'filename': message.file.name if message.file else None
        }
```

### Конфигурация и деплой

```yaml
# docker-compose.yml
version: '3.8'
services:
  parser:
    build: .
    environment:
      - TELEGRAM_API_ID=${API_ID}
      - TELEGRAM_API_HASH=${API_HASH}
    volumes:
      - ./sessions:/app/sessions:rw
      - ./media:/app/media:rw
    deploy:
      replicas: 3
    depends_on:
      - redis
      - postgres
  
  redis:
    image: redis:alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
  
  postgres:
    image: postgres:14
    environment:
      POSTGRES_DB: telegram_parser
    volumes:
      - pg_data:/var/lib/postgresql/data

volumes:
  pg_data:
```

### Graceful shutdown и восстановление

```python
import signal
import sys

class GracefulKiller:
    def __init__(self, client_manager):
        self.client_manager = client_manager
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        signal.signal(signal.SIGINT, self.exit_gracefully)
    
    def exit_gracefully(self, signum, frame):
        print("Получен сигнал завершения, сохраняем состояние...")
        
        # Сохранение offset'ов для каждого чата
        for session_name, data in self.client_manager.clients.items():
            # Сохранение последних прочитанных message_id
            pass
        
        sys.exit(0)

# Использование
killer = GracefulKiller(session_manager)
```

---

## Заключение и рекомендации

### Best Practices
1. **Начинайте с тестового аккаунта** - никогда не используйте основной номер телефона
2. **Реализуйте circuit breaker** - при получении FloodWait > 1 часа отключайте сессию на сутки
3. **Шифруйте .session файлы** - они содержат аутентификационные ключи
4. **Используйте connection pooling** - Telethon поддерживает `connection=ConnectionTcpFull`
5. **Мониторьте `session.save()`** - регулярно обновляйте string session для бэкапа

### Юридические аспекты
- Соблюдайте Terms of Service Telegram
- Не храните персональные данные без согласия
- Реализуйте механизм удаления данных по запросу (GDPR/CCPA)

### Оптимизация производительности
- Для high-load сценариев используйте `telethon.sync` только для инициализации
- Применяйте `limit` в `iter_messages` пачками по 100-300 сообщений
- Кэшируйте `get_entity()` результаты (сущности редко меняются)

**Важно:** Данное руководство создано в образовательных целях. Массовый парсинг может нарушать политику Telegram и привести к перманентному бану номера телефона.