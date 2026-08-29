import os
import sys
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel
from rich.console import Console
from rich.panel import Panel

console = Console()

async def fetch_5_chats():
    load_dotenv(Path(__file__).parent / ".env")
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    session_path = Path(__file__).parent / "personal_account"
    client = TelegramClient(str(session_path), int(api_id), api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("Ошибка: клиент не авторизован")
        return

    dialogs = []
    async for d in client.iter_dialogs(limit=10):
        dialogs.append(d)

    # Выбираем 5 разнообразных чатов (ЛС/боты/каналы)
    selected = dialogs[:5]

    all_chats_data = []

    for idx, d in enumerate(selected, 1):
        chat_title = d.name or "Без имени"
        chat_id = d.id
        console.print(Panel(f"[bold yellow]Чат {idx}/5:[/bold yellow] [bold green]{chat_title}[/bold green] (ID: {chat_id})", border_style="cyan"))

        messages = []
        async for msg in client.iter_messages(d.entity, limit=20):
            sender = await msg.get_sender()
            sender_name = "Вы" if msg.out else (getattr(sender, "first_name", "") or getattr(sender, "title", "") or "Неизвестный")
            date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else ""
            
            raw_text = msg.text or ""
            text_preview = raw_text.replace("\n", " ")[:120] if raw_text else ("📎 [Медиа/Вложение]" if msg.media else "[Пустое сообщение]")
            
            color = "cyan" if msg.out else "white"
            console.print(f"  [dim]{date_str}[/dim] [{color}][bold]{sender_name}:[/bold][/{color}] {text_preview}")

            messages.append({
                "id": msg.id,
                "date": date_str,
                "sender": sender_name,
                "is_out": msg.out,
                "text": raw_text,
                "has_media": bool(msg.media)
            })

        all_chats_data.append({
            "chat_title": chat_title,
            "chat_id": chat_id,
            "messages_count": len(messages),
            "messages": messages
        })

    # Сохраняем в exports
    exports_dir = Path(__file__).parent / "exports"
    exports_dir.mkdir(exist_ok=True)
    out_file = exports_dir / "sample_5_chats_20_messages.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_chats_data, f, ensure_ascii=False, indent=2)

    console.print(f"\n[bold green]✅ Все данные успешно извлечены и сохранены в:[/bold green] [cyan]{out_file}[/cyan]")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(fetch_5_chats())
