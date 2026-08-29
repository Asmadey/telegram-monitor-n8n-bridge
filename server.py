import os
import sys
import json
import uuid
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors
from telethon.tl.types import User, Chat, Channel

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
DB_FILE = BASE_DIR / "storage.db"
MONITORS_OLD_FILE = BASE_DIR / "monitors.json"
EXPORTS_DIR = BASE_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

load_dotenv(ENV_FILE)

API_ID = os.getenv("TELEGRAM_API_ID", "")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

SESSION_PATH = BASE_DIR / "personal_account"
client: Optional[TelegramClient] = None

auth_state = {
    "phone": None,
    "phone_code_hash": None
}

# ==================== SQLite Хранилище ====================

def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Таблица каналов мониторинга
    cur.execute("""
    CREATE TABLE IF NOT EXISTS monitors (
        id TEXT PRIMARY KEY,
        chat_target TEXT NOT NULL,
        chat_title TEXT,
        chat_username TEXT,
        chat_id INTEGER,
        interval_minutes INTEGER DEFAULT 60,
        limit_count INTEGER DEFAULT 20,
        offset_hours INTEGER DEFAULT 24,
        is_active INTEGER DEFAULT 1,
        last_checked TEXT,
        last_sent_message_id INTEGER DEFAULT 0,
        prompt TEXT,
        created_at TEXT
    );
    """)

    # Таблица отправленных сообщений (для строгой дедубликации)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        date TEXT,
        sender TEXT,
        text TEXT,
        views INTEGER,
        post_url TEXT,
        sent_at TEXT,
        UNIQUE(chat_id, message_id)
    );
    """)

    # Таблица логов событий и отправок
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        chat_title TEXT,
        chat_id INTEGER,
        messages_count INTEGER DEFAULT 0,
        status TEXT NOT NULL,
        details TEXT
    );
    """)

    # Таблица глобальных настроек (Webhook и др.)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()

    # Миграция колонок (если база уже создана)
    try:
        cur.execute("ALTER TABLE sent_messages ADD COLUMN reactions_count INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE monitors ADD COLUMN prompt TEXT")
        conn.commit()
    except Exception:
        pass

    # Миграция из monitors.json, если он еще существует
    if MONITORS_OLD_FILE.exists():
        try:
            with open(MONITORS_OLD_FILE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                
            webhook_url = old_data.get("webhook_url", "")
            auto_webhook = "1" if old_data.get("auto_webhook_enabled", True) else "0"
            set_setting("webhook_url", webhook_url)
            set_setting("auto_webhook_enabled", auto_webhook)

            for m in old_data.get("monitors", []):
                cur.execute("""
                INSERT OR IGNORE INTO monitors (
                    id, chat_target, chat_title, chat_username, chat_id,
                    interval_minutes, limit_count, offset_hours, is_active,
                    last_checked, last_sent_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    m.get("id"), m.get("chat_target"), m.get("chat_title"), m.get("chat_username"),
                    m.get("chat_id"), m.get("interval_minutes", 60), m.get("limit", 20),
                    m.get("offset_hours", 24), 1 if m.get("is_active", True) else 0,
                    m.get("last_checked"), m.get("last_sent_message_id", 0), m.get("created_at")
                ))

                chat_id = m.get("chat_id")
                for s_id in m.get("sent_ids", []):
                    cur.execute("""
                    INSERT OR IGNORE INTO sent_messages (chat_id, message_id, sent_at)
                    VALUES (?, ?, ?)
                    """, (chat_id, s_id, datetime.now(timezone.utc).isoformat()))

            conn.commit()
            MONITORS_OLD_FILE.rename(BASE_DIR / "monitors.json.bak")
            add_log("SYSTEM", "Миграция данных из monitors.json в SQLite storage.db успешно завершена", "SUCCESS")
        except Exception as e:
            print(f"⚠️ Ошибка миграции из JSON в SQLite: {e}")

    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def add_log(event_type: str, details: str, status: str = "INFO", chat_title: Optional[str] = None, chat_id: Optional[int] = None, messages_count: int = 0):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO logs (timestamp, event_type, chat_title, chat_id, messages_count, status, details)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        event_type,
        chat_title,
        chat_id,
        messages_count,
        status,
        details
    ))
    conn.commit()
    conn.close()

def get_client() -> TelegramClient:
    global client, API_ID, API_HASH
    if client is None:
        if not API_ID or not API_HASH:
            raise HTTPException(status_code=400, detail="TELEGRAM_API_ID и TELEGRAM_API_HASH не настроены в .env")
        client = TelegramClient(str(SESSION_PATH), int(API_ID), API_HASH)
    return client

def update_env_file(api_id: str, api_hash: str, phone: Optional[str] = None):
    global API_ID, API_HASH
    API_ID = api_id.strip()
    API_HASH = api_hash.strip()
    
    lines = [
        f"TELEGRAM_API_ID={API_ID}\n",
        f"TELEGRAM_API_HASH={API_HASH}\n"
    ]
    if phone:
        lines.append(f"TELEGRAM_PHONE={phone.strip()}\n")
    
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)
    
    os.environ["TELEGRAM_API_ID"] = API_ID
    os.environ["TELEGRAM_API_HASH"] = API_HASH

# ==================== Дедубликация сообщений в SQLite ====================

def filter_and_save_new_messages(chat_id: int, messages: List[Dict[str, Any]], mark_sent: bool = True) -> List[Dict[str, Any]]:
    if not messages:
        return []
    
    conn = get_db()
    cur = conn.cursor()
    
    # Получаем уже отправленные ID
    msg_ids = [m["id"] for m in messages if m.get("id")]
    placeholders = ",".join("?" for _ in msg_ids)
    cur.execute(f"SELECT message_id FROM sent_messages WHERE chat_id = ? AND message_id IN ({placeholders})", [chat_id] + msg_ids)
    existing_sent = {row["message_id"] for row in cur.fetchall()}

    new_messages = []
    for msg in messages:
        m_id = msg.get("id")
        if m_id and m_id not in existing_sent:
            new_messages.append(msg)

    if mark_sent and new_messages:
        now_str = datetime.now(timezone.utc).isoformat()
        max_id = max(m["id"] for m in new_messages)
        for msg in new_messages:
            cur.execute("""
            INSERT OR IGNORE INTO sent_messages (chat_id, message_id, date, sender, text, views, reactions_count, post_url, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id,
                msg["id"],
                msg.get("date"),
                msg.get("sender"),
                msg.get("text", "")[:500],
                msg.get("views"),
                msg.get("reactions_count", 0),
                msg.get("post_url"),
                now_str
            ))
        
        # Обновляем last_sent_message_id в таблице monitors
        cur.execute("""
        UPDATE monitors 
        SET last_sent_message_id = MAX(last_sent_message_id, ?) 
        WHERE chat_id = ?
        """, (max_id, chat_id))
        conn.commit()

    conn.close()
    return new_messages

def get_sent_ids_count(chat_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM sent_messages WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0

# ==================== Фоновый планировщик ====================

scheduler_running = True

async def background_monitor_worker():
    while scheduler_running:
        try:
            await asyncio.sleep(30)
            if client is None or not client.is_connected():
                continue
            if not await client.is_user_authorized():
                continue

            webhook_url = get_setting("webhook_url", "").strip()
            auto_webhook = get_setting("auto_webhook_enabled", "1") == "1"
            now = datetime.now(timezone.utc)

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM monitors WHERE is_active = 1")
            active_monitors = [dict(row) for row in cur.fetchall()]
            conn.close()

            for monitor in active_monitors:
                last_checked_str = monitor.get("last_checked")
                interval_min = monitor.get("interval_minutes", 60)

                should_run = False
                if not last_checked_str:
                    should_run = True
                else:
                    try:
                        last_checked = datetime.fromisoformat(last_checked_str)
                        if now >= last_checked + timedelta(minutes=interval_min):
                            should_run = True
                    except Exception:
                        should_run = True

                if should_run:
                    print(f"⏰ [Scheduler] Опрос: {monitor.get('chat_title')} ({monitor.get('chat_target')})")
                    try:
                        res = await fetch_chat_messages(
                            target=monitor.get("chat_target"),
                            limit=monitor.get("limit_count", 20),
                            offset_hours=monitor.get("offset_hours")
                        )
                        
                        # Обновляем last_checked
                        conn = get_db()
                        conn.cursor().execute("UPDATE monitors SET last_checked = ? WHERE id = ?", (now.isoformat(), monitor["id"]))
                        conn.commit()
                        conn.close()

                        raw_messages = res.get("messages", [])
                        new_messages = filter_and_save_new_messages(monitor["chat_id"], raw_messages, mark_sent=True)

                        if webhook_url and auto_webhook:
                            if new_messages:
                                print(f"📤 [Scheduler] Отправка {len(new_messages)} новых постов в n8n из '{monitor.get('chat_title')}'")
                                payload_to_send = {
                                    **res,
                                    "messages_count": len(new_messages),
                                    "messages": new_messages
                                }
                                try:
                                    await send_to_n8n_webhook(webhook_url, payload_to_send, monitor.get("prompt"))
                                    add_log(
                                        event_type="WEBHOOK_SENT",
                                        details=f"Отправлено {len(new_messages)} новых сообщений в n8n",
                                        status="SUCCESS",
                                        chat_title=monitor.get("chat_title"),
                                        chat_id=monitor.get("chat_id"),
                                        messages_count=len(new_messages)
                                    )
                                except Exception as we:
                                    add_log(
                                        event_type="WEBHOOK_ERROR",
                                        details=f"Ошибка отправки в n8n: {str(we)}",
                                        status="ERROR",
                                        chat_title=monitor.get("chat_title"),
                                        chat_id=monitor.get("chat_id")
                                    )
                            else:
                                add_log(
                                    event_type="SCHEDULER_POLL",
                                    details=f"Опрос завершен. Все {len(raw_messages)} сообщений уже были отправлены ранее (0 новых).",
                                    status="SKIPPED_DEDUP",
                                    chat_title=monitor.get("chat_title"),
                                    chat_id=monitor.get("chat_id")
                                )

                    except Exception as e:
                        print(f"⚠️ [Scheduler Error] {monitor.get('chat_target')}: {e}")
                        add_log(
                            event_type="POLL_ERROR",
                            details=f"Ошибка извлечения: {str(e)}",
                            status="ERROR",
                            chat_title=monitor.get("chat_title"),
                            chat_id=monitor.get("chat_id")
                        )
                    
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            break
        except Exception as e:
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_running, client
    init_db()
    try:
        c = get_client()
        await c.connect()
        if await c.is_user_authorized():
            me = await c.get_me()
            print(f"🚀 Telegram Client подключен к аккаунту: {me.first_name} (@{me.username or 'no_user'})")
            add_log("AUTH", f"Telegram Client запущен под аккаунтом {me.first_name} (@{me.username})", "SUCCESS")
        else:
            print("⚠️ Telegram-клиент ожидает авторизации через веб-интерфейс.")
    except Exception as e:
        print(f"⚠️ Инициализация Telegram клиента: {e}")

    worker_task = asyncio.create_task(background_monitor_worker())
    yield
    scheduler_running = False
    worker_task.cancel()
    if client and client.is_connected():
        await client.disconnect()
    print("🛑 Сервер остановлен.")

app = FastAPI(
    title="Telegram MTProto Monitor & n8n Bridge (SQLite Edition)",
    description="Веб-интерфейс, авторизация MTProto, дедубликация, SQLite логи и интеграция с n8n",
    version="2.3.0",
    lifespan=lifespan
)

# ==================== Вспомогательные функции ====================

def clean_target(target: str):
    target = target.strip()
    if "t.me/" in target:
        target = target.split("t.me/")[-1].replace("+", "").replace("/", "")
    if target.startswith("@"):
        target = target[1:]
    if target.startswith("-") or target.isdigit():
        try:
            return int(target)
        except ValueError:
            pass
    return target

def build_post_url(entity, msg_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    entity_id = getattr(entity, "id", 0)
    if entity_id:
        clean_id = str(entity_id).replace("-100", "").replace("-", "")
        return f"https://t.me/c/{clean_id}/{msg_id}"
    return ""

async def fetch_chat_messages(target: str, limit: int = 20, offset_hours: Optional[int] = None) -> Dict[str, Any]:
    c = get_client()
    if not await c.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram аккаунт не авторизован")

    cleaned = clean_target(target)
    entity = await c.get_entity(cleaned)

    title = getattr(entity, "title", None) or f"{getattr(entity, 'first_name', '')} {getattr(entity, 'last_name', '')}".strip() or str(target)
    username = getattr(entity, "username", None)
    chat_id = getattr(entity, "id", 0)

    time_cutoff = None
    if offset_hours:
        time_cutoff = datetime.now(timezone.utc) - timedelta(hours=offset_hours)

    messages = []
    async for msg in c.iter_messages(entity, limit=limit):
        if time_cutoff and msg.date and msg.date < time_cutoff:
            break

        text = (msg.text or "").strip()
        # Исключаем сообщения без текста (чистые картинки/стикеры/вложения без описания)
        if not text or text == "📎 [Медиа/Вложение]":
            continue

        sender = await msg.get_sender()
        sender_name = "Вы" if msg.out else (getattr(sender, "first_name", "") or getattr(sender, "title", "") or title)
        date_str = msg.date.isoformat() if msg.date else ""
        post_url = build_post_url(entity, msg.id)

        # Подсчет реакций у поста
        reactions_count = 0
        reactions_details = []
        if getattr(msg, "reactions", None) and getattr(msg.reactions, "results", None):
            for r in msg.reactions.results:
                count = getattr(r, "count", 0)
                reactions_count += count
                emoticon = getattr(getattr(r, "reaction", None), "emoticon", "")
                reactions_details.append({
                    "emoji": emoticon,
                    "count": count
                })

        messages.append({
            "id": msg.id,
            "date": date_str,
            "sender": sender_name,
            "sender_id": msg.sender_id,
            "is_outgoing": msg.out,
            "text": text,
            "has_media": bool(msg.media),
            "views": getattr(msg, "views", None),
            "forwards": getattr(msg, "forwards", None),
            "reactions_count": reactions_count,
            "reactions": reactions_details,
            "post_url": post_url
        })

    return {
        "chat_id": chat_id,
        "chat_title": title,
        "chat_username": username,
        "messages_count": len(messages),
        "messages": messages
    }

async def call_openrouter(text: str, custom_prompt: Optional[str] = None) -> Optional[str]:
    api_key = get_setting("openrouter_api_key", "").strip()
    if not api_key:
        return None
    base_url = get_setting("openrouter_base_url", "https://openrouter.ai/api/v1").rstrip("/")
    model = get_setting("openrouter_model", "google/gemini-2.0-flash-001").strip()
    system_prompt = custom_prompt or get_setting(
        "openrouter_system_prompt",
        "Выдели ключевую суть сообщения, ключевые технологии, условия и теги. Будь краток."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://telegram-monitor.local",
        "X-Title": "Telegram MTProto Monitor"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=35.0) as http_client:
            url = f"{base_url}/chat/completions"
            resp = await http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices and len(choices) > 0:
                return choices[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"⚠️ Ошибка OpenRouter API: {e}")
        add_log("OPENROUTER_ERROR", f"Ошибка генерации ответа OpenRouter: {str(e)}", "ERROR")
    return None

async def process_messages_batch_with_llm(messages: List[Dict[str, Any]], custom_prompt: Optional[str] = None) -> Optional[str]:
    is_enabled = get_setting("openrouter_enabled", "0") == "1"
    api_key = get_setting("openrouter_api_key", "").strip()
    if not is_enabled or not api_key:
        return None

    # Формируем компактный массив строго из 3 атрибутов (ID, пост, ссылка)
    post_items = []
    for item in messages:
        text = (item.get("text") or "").strip()
        if text and text != "📎 [Медиа/Вложение]":
            post_items.append({
                "ID": str(item.get("id", "")),
                "пост": text,
                "ссылка": item.get("post_url", "") or f"https://t.me/{item.get('chat_username', 'c')}/{item.get('id', '')}"
            })

    if not post_items:
        return None

    base_url = get_setting("openrouter_base_url", "https://openrouter.ai/api/v1").rstrip("/")
    model = get_setting("openrouter_model", "google/gemini-2.0-flash-001").strip()
    
    # Приоритет: промпт канала -> глобальный системный промпт -> дефолт
    effective_prompt = (custom_prompt or "").strip()
    if not effective_prompt:
        effective_prompt = get_setting(
            "openrouter_system_prompt",
            "Выдели ключевую суть сообщений, ключевые технологии, условия и теги. Будь краток."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://telegram-monitor.local",
        "X-Title": "Telegram MTProto Monitor"
    }

    # В LLM отправляется строго структура { "post": [ { "ID": "...", "пост": "...", "ссылка": "..." } ] }
    user_payload = {
        "post": post_items
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": effective_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)}
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as http_client:
            url = f"{base_url}/chat/completions"
            resp = await http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices and len(choices) > 0:
                ai_content = choices[0].get("message", {}).get("content", "").strip()
                return ai_content
    except Exception as e:
        print(f"⚠️ Ошибка OpenRouter Batch: {e}")
        add_log("OPENROUTER_ERROR", f"Ошибка обработки батча сообщений через LLM: {str(e)}", "ERROR")
    return None

async def send_telegram_bot_message(text: str, custom_chat_id: Optional[str] = None) -> bool:
    token = get_setting("telegram_bot_token", "").strip()
    chat_id = custom_chat_id or get_setting("telegram_forward_chat_id", "").strip()
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.post(url, json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        print(f"⚠️ Ошибка отправки Telegram Bot: {e}")
        add_log("TG_BOT_ERROR", f"Ошибка отправки через Telegram Bot API: {str(e)}", "ERROR")
        return False

async def send_to_n8n_webhook(webhook_url: str, payload: Dict[str, Any], channel_prompt: Optional[str] = None) -> Dict[str, Any]:
    # Если включен OpenRouter и есть сообщения, генерируем ai_analysis по компактному батчу (ID, пост, ссылка)
    if "messages" in payload and payload["messages"]:
        ai_res = await process_messages_batch_with_llm(payload["messages"], channel_prompt)
        if ai_res:
            payload["ai_analysis"] = ai_res

    # Если включена прямая пересылка в Telegram-канал через Bot API
    if get_setting("telegram_forward_enabled", "0") == "1" and "messages" in payload and payload["messages"]:
        try:
            chat_title = payload.get("chat_title", "Источник")
            ai_summary = payload.get("ai_analysis")
            msg_lines = [f"📢 <b>Новые посты: {chat_title}</b> ({len(payload['messages'])} шт.)\n"]
            if ai_summary:
                msg_lines.append(f"🤖 <b>AI Анализ:</b>\n{ai_summary}\n")
            for m in payload["messages"][:5]:
                m_text = (m.get("text") or "")[:250]
                m_url = m.get("post_url", "")
                link_html = f" — <a href='{m_url}'>🔗 Источник</a>" if m_url else ""
                msg_lines.append(f"• {m_text}{link_html}\n")
            
            full_msg = "\n".join(msg_lines)
            await send_telegram_bot_message(full_msg)
        except Exception as fe:
            print(f"⚠️ Ошибка фоновой пересылки ботом: {fe}")

    async with httpx.AsyncClient(timeout=25.0) as http_client:
        data_to_send = {
            "source": "telethon_monitor",
            "event": "telegram_messages_batch",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload
        }
        resp = await http_client.post(webhook_url, json=data_to_send)
        resp.raise_for_status()
        return {
            "status": "success",
            "status_code": resp.status_code,
            "response_text": resp.text[:200]
        }

# ==================== Pydantic Схемы ====================

class SettingsUpdateRequest(BaseModel):
    api_id: str
    api_hash: str
    phone: Optional[str] = None

class SendCodeRequest(BaseModel):
    phone: str

class SignInRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: Optional[str] = None
    password: Optional[str] = None

class MonitorCreateRequest(BaseModel):
    chat_target: str
    interval_minutes: int = 60
    limit: int = 20
    offset_hours: Optional[int] = 24
    is_active: bool = True
    prompt: Optional[str] = None

class MonitorUpdateRequest(BaseModel):
    interval_minutes: Optional[int] = None
    limit: Optional[int] = None
    offset_hours: Optional[int] = None
    is_active: Optional[bool] = None
    prompt: Optional[str] = None

class WebhookConfigRequest(BaseModel):
    webhook_url: str
    auto_webhook_enabled: bool = True

class DirectReadRequest(BaseModel):
    chat: str
    limit: int = 20
    offset_hours: Optional[int] = None

class OpenRouterConfigRequest(BaseModel):
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str
    model: str = "google/gemini-2.0-flash-001"
    system_prompt: Optional[str] = "Выдели ключевую суть сообщения, ключевые технологии, условия и теги. Будь краток."
    is_enabled: bool = False

class OpenRouterTestRequest(BaseModel):
    sample_text: Optional[str] = "Требуется Senior Python разработчик с опытом FastAPI и Telegram API. Зарплата от $4000."

class TelegramForwardConfigRequest(BaseModel):
    bot_token: str
    sender_id: str
    is_enabled: bool = False

# ==================== API Эндпоинты ====================

@app.get("/health")
async def health_check():
    c = get_client()
    if not c.is_connected():
        await c.connect()
    is_auth = await c.is_user_authorized()
    user_info = None
    if is_auth:
        me = await c.get_me()
        user_info = {
            "id": me.id,
            "first_name": me.first_name,
            "username": me.username
        }
    return {
        "status": "online",
        "authorized": is_auth,
        "user": user_info
    }

# --- Настройки MTProto ---

@app.get("/api/settings")
async def get_settings():
    c = get_client()
    if not c.is_connected():
        await c.connect()
    is_auth = await c.is_user_authorized()
    user_info = None
    if is_auth:
        me = await c.get_me()
        user_info = {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": getattr(me, "last_name", ""),
            "username": me.username,
            "phone": getattr(me, "phone", "")
        }
    
    return {
        "api_id": API_ID,
        "api_hash": API_HASH,
        "is_authorized": is_auth,
        "user": user_info
    }

@app.post("/api/settings")
async def save_settings(req: SettingsUpdateRequest):
    global client
    update_env_file(req.api_id, req.api_hash, req.phone)
    if client and client.is_connected():
        await client.disconnect()
    client = TelegramClient(str(SESSION_PATH), int(API_ID), API_HASH)
    await client.connect()
    is_auth = await client.is_user_authorized()
    add_log("SETTINGS", "Обновлены ключи MTProto API в .env", "SUCCESS")
    return {
        "status": "saved",
        "api_id": API_ID,
        "is_authorized": is_auth
    }

# --- Интерактивная веб-авторизация ---

@app.post("/api/auth/send-code")
async def auth_send_code(req: SendCodeRequest):
    c = get_client()
    if not c.is_connected():
        await c.connect()

    phone = req.phone.strip()
    try:
        sent = await c.send_code_request(phone)
        auth_state["phone"] = phone
        auth_state["phone_code_hash"] = sent.phone_code_hash
        add_log("AUTH", f"Запрошен код подтверждения для {phone}", "INFO")
        return {
            "status": "code_sent",
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "message": f"Код подтверждения отправлен в приложение Telegram для {phone}"
        }
    except errors.AuthRestartError:
        await asyncio.sleep(1)
        sent = await c.send_code_request(phone)
        auth_state["phone"] = phone
        auth_state["phone_code_hash"] = sent.phone_code_hash
        return {
            "status": "code_sent",
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "message": f"Код подтверждения отправлен в приложение Telegram для {phone}"
        }
    except Exception as e:
        add_log("AUTH", f"Ошибка отправки кода: {str(e)}", "ERROR")
        raise HTTPException(status_code=400, detail=f"Ошибка отправки кода: {str(e)}")

@app.post("/api/auth/sign-in")
async def auth_sign_in(req: SignInRequest):
    c = get_client()
    if not c.is_connected():
        await c.connect()

    phone = req.phone.strip() or auth_state.get("phone")
    phone_code_hash = req.phone_code_hash or auth_state.get("phone_code_hash")
    code = req.code.strip()

    try:
        await c.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except errors.SessionPasswordNeededError:
        if not req.password:
            return {
                "status": "2fa_required",
                "phone": phone,
                "phone_code_hash": phone_code_hash,
                "message": "Включена двухфакторная аутентификация (2FA). Введите ваш пароль."
            }
        try:
            await c.sign_in(password=req.password)
        except Exception as e:
            add_log("AUTH", f"Неверный 2FA пароль: {str(e)}", "ERROR")
            raise HTTPException(status_code=400, detail=f"Неверный пароль 2FA: {str(e)}")
    except Exception as e:
        add_log("AUTH", f"Ошибка входа: {str(e)}", "ERROR")
        raise HTTPException(status_code=400, detail=f"Ошибка авторизации: {str(e)}")

    if await c.is_user_authorized():
        me = await c.get_me()
        add_log("AUTH", f"Успешная авторизация пользователя {me.first_name} (@{me.username})", "SUCCESS")
        return {
            "status": "authorized",
            "message": "Авторизация успешно завершена!",
            "user": {
                "id": me.id,
                "first_name": me.first_name,
                "last_name": getattr(me, "last_name", ""),
                "username": me.username
            }
        }
    else:
        raise HTTPException(status_code=400, detail="Не удалось завершить авторизацию.")

@app.post("/api/auth/logout")
async def auth_logout():
    c = get_client()
    if not c.is_connected():
        await c.connect()
    try:
        await c.log_out()
    except Exception:
        pass
    if SESSION_PATH.with_suffix(".session").exists():
        SESSION_PATH.with_suffix(".session").unlink()
    add_log("AUTH", "Выход из аккаунта и сброс сессии", "INFO")
    return {"status": "logged_out", "message": "Сессия успешно сброшена."}

# --- Диалоги, Чтение, Мониторинг в SQLite ---

@app.get("/dialogs")
async def get_dialogs(limit: int = Query(50, ge=1, le=100)):
    c = get_client()
    if not await c.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram-клиент не авторизован")

    dialogs = []
    async for d in c.iter_dialogs(limit=limit):
        entity = d.entity
        entity_type = "user" if isinstance(entity, User) else "group" if isinstance(entity, Chat) else "channel"
        dialogs.append({
            "id": d.id,
            "name": d.name or "Без названия",
            "username": getattr(entity, "username", None),
            "type": entity_type,
            "unread_count": d.unread_count
        })
    return {"total": len(dialogs), "dialogs": dialogs}

@app.get("/api/messages")
async def get_saved_messages(limit: int = Query(100, ge=1, le=500)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT sm.*, m.chat_title, m.chat_username 
        FROM sent_messages sm
        LEFT JOIN monitors m ON sm.chat_id = m.chat_id
        ORDER BY sm.id DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {"total": len(rows), "messages": rows}

@app.get("/api/monitors")
async def get_monitors():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM monitors ORDER BY created_at DESC")
    monitors = []
    for row in cur.fetchall():
        m_dict = dict(row)
        m_dict["limit"] = m_dict["limit_count"]
        m_dict["is_active"] = bool(m_dict["is_active"])
        # Считаем количество отправленных сообщений из sent_messages
        m_dict["sent_count"] = get_sent_ids_count(m_dict["chat_id"])

        # Расчет следующей отправки по времени последней проверки/вебхука + интервал
        last_checked_str = m_dict.get("last_checked")
        interval_min = m_dict.get("interval_minutes", 60)
        if last_checked_str:
            try:
                ref_time = datetime.fromisoformat(last_checked_str)
                next_run_dt = ref_time + timedelta(minutes=interval_min)
                m_dict["next_run"] = next_run_dt.isoformat()
                diff_sec = int((next_run_dt - datetime.now(timezone.utc)).total_seconds())
                m_dict["seconds_until_next_run"] = max(0, diff_sec)
            except Exception:
                m_dict["next_run"] = None
                m_dict["seconds_until_next_run"] = 0
        else:
            m_dict["next_run"] = None
            m_dict["seconds_until_next_run"] = 0

        monitors.append(m_dict)
    conn.close()

    return {
        "webhook_url": get_setting("webhook_url", ""),
        "auto_webhook_enabled": get_setting("auto_webhook_enabled", "1") == "1",
        "monitors": monitors
    }

@app.post("/api/monitors")
async def add_monitor(req: MonitorCreateRequest):
    c = get_client()
    if not await c.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram-клиент не авторизован")

    cleaned = clean_target(req.chat_target)
    try:
        entity = await c.get_entity(cleaned)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось найти чат '{req.chat_target}': {str(e)}")

    title = getattr(entity, "title", None) or f"{getattr(entity, 'first_name', '')} {getattr(entity, 'last_name', '')}".strip() or str(req.chat_target)
    username = getattr(entity, "username", None)
    chat_id = getattr(entity, "id", 0)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM monitors WHERE chat_id = ? OR chat_target = ?", (chat_id, req.chat_target))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Этот чат/канал уже добавлен в мониторинг")

    new_id = str(uuid.uuid4())[:8]
    now_iso = datetime.now(timezone.utc).isoformat()

    cur.execute("""
    INSERT INTO monitors (
        id, chat_target, chat_title, chat_username, chat_id,
        interval_minutes, limit_count, offset_hours, is_active,
        last_checked, last_sent_message_id, prompt, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
    """, (
        new_id, req.chat_target, title, username, chat_id,
        req.interval_minutes, req.limit, req.offset_hours, 1 if req.is_active else 0,
        req.prompt.strip() if req.prompt else None,
        now_iso
    ))
    conn.commit()
    conn.close()

    add_log("CHANNEL_ADDED", f"Добавлен канал '{title}' ({req.chat_target})", "SUCCESS", title, chat_id)

    return {
        "id": new_id,
        "chat_target": req.chat_target,
        "chat_title": title,
        "chat_username": username,
        "chat_id": chat_id,
        "interval_minutes": req.interval_minutes,
        "limit": req.limit,
        "offset_hours": req.offset_hours,
        "is_active": req.is_active,
        "prompt": req.prompt,
        "last_checked": None,
        "last_sent_message_id": 0,
        "sent_count": 0,
        "created_at": now_iso
    }

@app.patch("/api/monitors/{monitor_id}")
async def update_monitor(monitor_id: str, req: MonitorUpdateRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Мониторинг не найден")

    m = dict(row)
    if req.interval_minutes is not None:
        m["interval_minutes"] = req.interval_minutes
    if req.limit is not None:
        m["limit_count"] = req.limit
    if req.offset_hours is not None:
        m["offset_hours"] = req.offset_hours
    if req.is_active is not None:
        m["is_active"] = 1 if req.is_active else 0
    if req.prompt is not None:
        m["prompt"] = req.prompt.strip() if req.prompt else ""

    cur.execute("""
    UPDATE monitors 
    SET interval_minutes = ?, limit_count = ?, offset_hours = ?, is_active = ?, prompt = ?
    WHERE id = ?
    """, (m["interval_minutes"], m["limit_count"], m["offset_hours"], m["is_active"], m.get("prompt"), monitor_id))
    conn.commit()
    conn.close()

    add_log("MONITOR_UPDATED", f"Обновлены настройки для '{m['chat_title']}': интервал {m['interval_minutes']} мин, лимит {m['limit_count']} постов", "INFO", m["chat_title"], m["chat_id"])
    return m

@app.post("/api/monitors/{monitor_id}/reset-dedup")
async def reset_monitor_dedup(monitor_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Мониторинг не найден")

    chat_id = row["chat_id"]
    cur.execute("DELETE FROM sent_messages WHERE chat_id = ?", (chat_id,))
    cur.execute("UPDATE monitors SET last_sent_message_id = 0 WHERE id = ?", (monitor_id,))
    conn.commit()
    conn.close()

    add_log("DEDUP_RESET", f"Сброшена история дедубликации для '{row['chat_title']}'", "INFO", row["chat_title"], chat_id)
    return {"status": "reset", "monitor_id": monitor_id, "message": "История отправки успешно очищена в SQLite"}

@app.delete("/api/monitors/{monitor_id}")
async def delete_monitor(monitor_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Мониторинг не найден")

    chat_title = row["chat_title"]
    cur.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    conn.commit()
    conn.close()

    add_log("CHANNEL_DELETED", f"Удален канал '{chat_title}'", "INFO", chat_title)
    return {"status": "deleted", "id": monitor_id}

@app.post("/api/monitors/{monitor_id}/run")
async def run_monitor_now(
    monitor_id: str,
    background_tasks: BackgroundTasks,
    force: bool = Query(False, description="Если True, отправляет все сообщения без дедубликации")
):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    monitor = dict(row)
    conn.close()

    try:
        res = await fetch_chat_messages(
            target=monitor.get("chat_target"),
            limit=monitor.get("limit_count", 20),
            offset_hours=monitor.get("offset_hours")
        )
        
        # Обновляем last_checked
        conn = get_db()
        conn.cursor().execute("UPDATE monitors SET last_checked = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), monitor["id"]))
        conn.commit()
        conn.close()

        raw_messages = res.get("messages", [])
        
        if force:
            new_messages = raw_messages
        else:
            new_messages = filter_and_save_new_messages(monitor["chat_id"], raw_messages, mark_sent=True)

        webhook_url = get_setting("webhook_url", "").strip()
        auto_webhook = get_setting("auto_webhook_enabled", "1") == "1"
        sent_to_webhook = False

        if webhook_url and auto_webhook and new_messages:
            payload_to_send = {
                **res,
                "messages_count": len(new_messages),
                "messages": new_messages
            }
            background_tasks.add_task(send_to_n8n_webhook, webhook_url, payload_to_send, monitor.get("prompt"))
            sent_to_webhook = True
            add_log(
                event_type="MANUAL_PUSH",
                details=f"Ручной запуск: отправлено {len(new_messages)} новых постов в n8n",
                status="SUCCESS",
                chat_title=monitor.get("chat_title"),
                chat_id=monitor.get("chat_id"),
                messages_count=len(new_messages)
            )
        elif len(raw_messages) > 0 and len(new_messages) == 0:
            add_log(
                event_type="MANUAL_RUN",
                details=f"Ручной запуск: новых сообщений нет, все {len(raw_messages)} уже отправлялись",
                status="SKIPPED_DEDUP",
                chat_title=monitor.get("chat_title"),
                chat_id=monitor.get("chat_id")
            )

        return {
            **res,
            "total_fetched": len(raw_messages),
            "new_messages_count": len(new_messages),
            "duplicates_filtered": len(raw_messages) - len(new_messages),
            "sent_to_webhook": sent_to_webhook,
            "last_sent_message_id": monitor.get("last_sent_message_id", 0)
        }
    except Exception as e:
        add_log(
            event_type="RUN_ERROR",
            details=f"Ошибка выполнения: {str(e)}",
            status="ERROR",
            chat_title=monitor.get("chat_title")
        )
        raise HTTPException(status_code=500, detail=f"Ошибка извлечения: {str(e)}")

# --- Webhook настройки ---

@app.get("/api/webhook")
async def get_webhook_config():
    return {
        "webhook_url": get_setting("webhook_url", ""),
        "auto_webhook_enabled": get_setting("auto_webhook_enabled", "1") == "1"
    }

@app.post("/api/webhook")
async def save_webhook_config(req: WebhookConfigRequest):
    set_setting("webhook_url", req.webhook_url.strip())
    set_setting("auto_webhook_enabled", "1" if req.auto_webhook_enabled else "0")
    add_log("SETTINGS", f"Сохранен Webhook URL: {req.webhook_url}", "SUCCESS")
    return {"status": "saved", "config": req}

@app.post("/api/webhook/test")
async def test_webhook():
    webhook_url = get_setting("webhook_url", "").strip()
    if not webhook_url:
        raise HTTPException(status_code=400, detail="Webhook URL n8n не настроен")

    sample_payload = {
        "chat_id": 1143063102,
        "chat_title": "Finder.work: работа и вакансии (Тест)",
        "chat_username": "theyseeku",
        "messages_count": 1,
        "messages": [
          {
            "id": 99999,
            "date": datetime.now(timezone.utc).isoformat(),
            "sender": "Finder.work",
            "is_outgoing": False,
            "text": "Тестовое сообщение из панели управления Telethon для проверки интеграции с n8n.",
            "has_media": False,
            "views": 100,
            "post_url": "https://t.me/theyseeku/38115"
          }
        ]
    }

    try:
        res = await send_to_n8n_webhook(webhook_url, sample_payload)
        add_log("WEBHOOK_TEST", "Тестовый вебхук успешно отправлен и принят n8n", "SUCCESS")
        return res
    except Exception as e:
        add_log("WEBHOOK_TEST", f"Ошибка отправки теста в n8n: {str(e)}", "ERROR")
        raise HTTPException(status_code=500, detail=f"Ошибка отправки на n8n: {str(e)}")

@app.post("/api/webhook/send-payload")
async def send_custom_payload(payload: Dict[str, Any]):
    webhook_url = get_setting("webhook_url", "").strip()
    if not webhook_url:
        raise HTTPException(status_code=400, detail="Webhook URL n8n не настроен")

    chat_title = payload.get("chat_title")
    chat_id = payload.get("chat_id")
    cnt = payload.get("messages_count", len(payload.get("messages", [])))

    try:
        res = await send_to_n8n_webhook(webhook_url, payload)
        add_log(
            event_type="WEBHOOK_SENT",
            details=f"Отправлен пакет из {cnt} сообщений по каналу '{chat_title or 'Без названия'}' в n8n",
            status="SUCCESS",
            chat_title=chat_title,
            chat_id=chat_id,
            messages_count=cnt
        )
        return res
    except Exception as e:
        add_log(
            event_type="WEBHOOK_ERROR",
            details=f"Ошибка отправки в n8n для '{chat_title or 'Канал'}': {str(e)}",
            status="ERROR",
            chat_title=chat_title,
            chat_id=chat_id
        )
        raise HTTPException(status_code=500, detail=f"Ошибка отправки: {str(e)}")

# --- OpenRouter AI Настройки ---

@app.get("/api/openrouter")
async def get_openrouter_config():
    api_key = get_setting("openrouter_api_key", "")
    masked_key = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else ("******" if api_key else "")
    return {
        "base_url": get_setting("openrouter_base_url", "https://openrouter.ai/api/v1"),
        "api_key_masked": masked_key,
        "has_key": bool(api_key),
        "model": get_setting("openrouter_model", "google/gemini-2.0-flash-001"),
        "system_prompt": get_setting("openrouter_system_prompt", "Выдели ключевую суть сообщения, ключевые технологии, условия и теги. Будь краток."),
        "is_enabled": get_setting("openrouter_enabled", "0") == "1"
    }

@app.post("/api/openrouter")
async def save_openrouter_config(req: OpenRouterConfigRequest):
    set_setting("openrouter_base_url", req.base_url.strip() or "https://openrouter.ai/api/v1")
    if req.api_key and req.api_key != "******" and "..." not in req.api_key:
        set_setting("openrouter_api_key", req.api_key.strip())
    set_setting("openrouter_model", req.model.strip() or "google/gemini-2.0-flash-001")
    set_setting("openrouter_system_prompt", req.system_prompt.strip() if req.system_prompt else "")
    set_setting("openrouter_enabled", "1" if req.is_enabled else "0")

    add_log("SETTINGS", f"Сохранены настройки OpenRouter (Модель: {req.model}, Активен: {req.is_enabled})", "SUCCESS")
    return {"status": "saved"}

@app.post("/api/openrouter/test")
async def test_openrouter(req: Optional[OpenRouterTestRequest] = None):
    api_key = get_setting("openrouter_api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API ключ OpenRouter не указан. Сохраните ключ перед тестом.")

    test_prompt = req.sample_text if req and req.sample_text else "Тестовое сообщение: требуется Senior Python разработчик с опытом FastAPI и Telegram API. Зарплата от $4000."
    res = await call_openrouter(test_prompt)
    if not res:
        raise HTTPException(status_code=500, detail="OpenRouter не вернул ответ. Проверьте API ключ, баланс или название модели.")

    add_log("OPENROUTER_TEST", f"Успешный тест OpenRouter ({get_setting('openrouter_model')})", "SUCCESS")
    return {
        "status": "success",
        "model": get_setting("openrouter_model"),
        "input": test_prompt,
        "response": res
    }

# --- Telegram Bot Forwarding Настройки ---

@app.get("/api/telegram-forward")
async def get_telegram_forward_config():
    token = get_setting("telegram_bot_token", "")
    masked_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else ("******" if token else "")
    return {
        "bot_token_masked": masked_token,
        "has_token": bool(token),
        "sender_id": get_setting("telegram_forward_chat_id", ""),
        "is_enabled": get_setting("telegram_forward_enabled", "0") == "1"
    }

@app.post("/api/telegram-forward")
async def save_telegram_forward_config(req: TelegramForwardConfigRequest):
    if req.bot_token and req.bot_token != "******" and "..." not in req.bot_token:
        set_setting("telegram_bot_token", req.bot_token.strip())
    set_setting("telegram_forward_chat_id", req.sender_id.strip())
    set_setting("telegram_forward_enabled", "1" if req.is_enabled else "0")

    add_log("SETTINGS", f"Сохранены настройки Telegram-бота (ID получателя: {req.sender_id}, Активен: {req.is_enabled})", "SUCCESS")
    return {"status": "saved"}

@app.post("/api/telegram-forward/test")
async def test_telegram_forward():
    token = get_setting("telegram_bot_token", "").strip()
    chat_id = get_setting("telegram_forward_chat_id", "").strip()
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Telegram bot API токен или ID отправителя не настроены")

    test_text = (
        "🚀 <b>Тестовое уведомление из Telethon Monitor!</b>\n\n"
        "Интеграция с Telegram-ботом работает корректно. Сюда будут поступать новые сообщения из отслеживаемых каналов."
    )
    ok = await send_telegram_bot_message(test_text, chat_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Не удалось отправить сообщение. Проверьте правильность токена, ID и права бота в канале/группе.")

    add_log("TG_BOT_TEST", f"Успешная тестовая отправка в канал/чат {chat_id}", "SUCCESS")
    return {"status": "success", "message": f"Тестовое сообщение успешно доставлено в {chat_id}"}

# --- API Логов (SQLite) ---

@app.get("/api/logs")
async def get_system_logs(
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = None
):
    conn = get_db()
    cur = conn.cursor()
    if status and status != "ALL":
        cur.execute("SELECT * FROM logs WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    
    logs = [dict(row) for row in cur.fetchall()]
    
    # Статистика
    cur.execute("SELECT COUNT(*) as total FROM logs")
    total_logs = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total_sent FROM sent_messages")
    total_sent_msgs = cur.fetchone()["total_sent"]
    conn.close()

    return {
        "total": total_logs,
        "total_sent_messages_db": total_sent_msgs,
        "logs": logs
    }

@app.delete("/api/logs")
async def clear_system_logs():
    conn = get_db()
    conn.cursor().execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    add_log("SYSTEM", "Журнал логов очищен пользователем", "INFO")
    return {"status": "cleared", "message": "Логи успешно очищены"}

@app.get("/", response_class=HTMLResponse)
@app.get("/feed", response_class=HTMLResponse)
@app.get("/messages", response_class=HTMLResponse)
@app.get("/channels", response_class=HTMLResponse)
@app.get("/integration", response_class=HTMLResponse)
@app.get("/logs", response_class=HTMLResponse)
async def serve_ui():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Интерфейс загружается...</h1>")
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
