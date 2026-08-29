import os
import sys
import json
import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

def get_client():
    load_dotenv(Path(__file__).parent / ".env")
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        console.print("[red]❌ Ошибка: Укажите TELEGRAM_API_ID и TELEGRAM_API_HASH в .env[/red]")
        sys.exit(1)

    session_path = Path(__file__).parent / "personal_account"
    return TelegramClient(str(session_path), int(api_id), api_hash)

async def list_dialogs(limit=25):
    client = get_client()
    await client.connect()

    if not await client.is_user_authorized():
        console.print("[red]❌ Аккаунт не авторизован! Сначала запустите: python auth.py[/red]")
        await client.disconnect()
        return

    table = Table(title=f"📋 Список последних диалогов (Лимит: {limit})", border_style="cyan")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Тип", style="bold magenta")
    table.add_column("Название / Контакт", style="bold green")
    table.add_column("Username", style="yellow")
    table.add_column("Непрочитано", justify="right", style="cyan")

    dialog_list = []
    async for dialog in client.iter_dialogs(limit=limit):
        entity = dialog.entity
        entity_type = "Пользователь" if isinstance(entity, User) else "Группа" if isinstance(entity, Chat) else "Канал"
        username = f"@{entity.username}" if getattr(entity, "username", None) else "-"
        unread = str(dialog.unread_count) if dialog.unread_count > 0 else "-"

        table.add_row(str(dialog.id), entity_type, dialog.name or "Без имени", username, unread)
        dialog_list.append({
            "id": dialog.id,
            "type": entity_type,
            "name": dialog.name,
            "username": username,
            "unread_count": dialog.unread_count
        })

    console.print(table)
    await client.disconnect()
    return dialog_list

async def read_chat(chat_target, limit=20, export_json=False):
    client = get_client()
    await client.connect()

    if not await client.is_user_authorized():
        console.print("[red]❌ Аккаунт не авторизован! Сначала запустите: python auth.py[/red]")
        await client.disconnect()
        return

    try:
        # Преобразуем числовой ID, если введен строкой
        target = int(chat_target) if (chat_target.startswith("-") or chat_target.isdigit()) else chat_target
        entity = await client.get_entity(target)
        title = getattr(entity, "title", None) or f"{getattr(entity, 'first_name', '')} {getattr(entity, 'last_name', '')}".strip() or str(chat_target)
    except Exception as e:
        console.print(f"[red]❌ Не удалось найти чат '{chat_target}': {e}[/red]")
        await client.disconnect()
        return

    console.print(Panel(f"Чтение сообщений из: [bold green]{title}[/bold green] (Лимит: {limit})", border_style="blue"))

    messages_data = []
    async for msg in client.iter_messages(entity, limit=limit):
        sender = await msg.get_sender()
        sender_name = "Вы" if msg.out else (getattr(sender, "first_name", "") or getattr(sender, "title", "") or "Неизвестный")
        date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "-"
        text_content = msg.text or ("📎 [Вложение / Медиа]" if msg.media else "[Пустое сообщение]")

        color = "cyan" if msg.out else "green"
        console.print(f"[dim]{date_str}[/dim] [{color}][bold]{sender_name}:[/bold][/{color}] {text_content}")

        messages_data.append({
            "id": msg.id,
            "date": date_str,
            "sender_id": msg.sender_id,
            "sender_name": sender_name,
            "is_outgoing": msg.out,
            "text": msg.text,
            "has_media": bool(msg.media)
        })

    if export_json:
        exports_dir = Path(__file__).parent / "exports"
        exports_dir.mkdir(exist_ok=True)
        filename = exports_dir / f"chat_{chat_target}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(messages_data, f, ensure_ascii=False, indent=2)
        console.print(f"\n[bold green]💾 Сообщения успешно экспортированы в:[/bold green] [cyan]{filename}[/cyan]")

    await client.disconnect()

async def search_messages(query, limit=20):
    client = get_client()
    await client.connect()

    if not await client.is_user_authorized():
        console.print("[red]❌ Аккаунт не авторизован! Сначала запустите: python auth.py[/red]")
        await client.disconnect()
        return

    console.print(Panel(f"🔍 Глобальный поиск по запросу: [bold yellow]'{query}'[/bold yellow] (Лимит: {limit})", border_style="yellow"))

    count = 0
    async for msg in client.iter_messages(None, search=query, limit=limit):
        chat = await msg.get_chat()
        chat_name = getattr(chat, "title", "") or getattr(chat, "first_name", "") or str(msg.chat_id)
        sender = await msg.get_sender()
        sender_name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or "Неизвестный"
        date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "-"

        console.print(f"[magenta][{chat_name}][/magenta] [dim]{date_str}[/dim] [bold]{sender_name}:[/bold] {msg.text}")
        count += 1

    if count == 0:
        console.print("[yellow]Ничего не найдено.[/yellow]")

    await client.disconnect()

def main():
    parser = argparse.ArgumentParser(description="Telegram Reader / Parser via Telethon")
    parser.add_argument("--list", action="store_true", help="Показать список последних диалогов")
    parser.add_argument("--limit", type=int, default=20, help="Количество диалогов/сообщений (по умолчанию: 20)")
    parser.add_argument("--chat", type=str, help="Username, номер телефона или ID чата для чтения сообщений")
    parser.add_argument("--search", type=str, help="Поиск сообщений по ключевому слову во всех чатах")
    parser.add_argument("--export", action="store_true", help="Экспортировать прочитанные сообщения в JSON")

    args = parser.parse_args()

    if args.list:
        asyncio.run(list_dialogs(limit=args.limit))
    elif args.chat:
        asyncio.run(read_chat(args.chat, limit=args.limit, export_json=args.export))
    elif args.search:
        asyncio.run(search_messages(args.search, limit=args.limit))
    else:
        # По умолчанию выводим список диалогов
        asyncio.run(list_dialogs(limit=args.limit))

if __name__ == "__main__":
    main()
