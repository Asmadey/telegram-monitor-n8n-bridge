import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from rich.console import Console
from rich.panel import Panel

console = Console()

async def authenticate():
    load_dotenv(Path(__file__).parent / ".env")
    
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")

    if not api_id or not api_hash:
        console.print("[red]❌ Ошибка: TELEGRAM_API_ID или TELEGRAM_API_HASH не найдены в .env![/red]")
        sys.exit(1)

    session_path = Path(__file__).parent / "personal_account"
    client = TelegramClient(str(session_path), int(api_id), api_hash)

    console.print(Panel.fit("[bold blue]Telegram User Account Authorization (Telethon)[/bold blue]", border_style="blue"))

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        console.print(f"[green]✅ Аккаунт уже авторизован![/green]")
        console.print(f"Имя: [bold]{me.first_name} {me.last_name or ''}[/bold]")
        console.print(f"Username: @{me.username or 'отсутствует'}")
        console.print(f"User ID: [cyan]{me.id}[/cyan]")
        console.print(f"Файл сессии: [dim]{session_path}.session[/dim]")
        await client.disconnect()
        return

    if not phone:
        phone = console.input("[yellow]Введите номер телефона аккаунта (например, +79991234567): [/yellow]").strip()

    console.print(f"Отправка запроса на код для [bold]{phone}[/bold]...")
    try:
        sent_code = await client.send_code_request(phone)
        code = console.input("[yellow]Введите код подтверждения из Telegram: [/yellow]").strip()
        
        try:
            await client.sign_in(phone, code)
        except errors.SessionPasswordNeededError:
            pwd = console.input("[yellow]Включена двухфакторная аутентификация (2FA). Введите пароль: [/yellow]", password=True).strip()
            await client.sign_in(password=pwd)

        if await client.is_user_authorized():
            me = await client.get_me()
            console.print(Panel(
                f"[bold green]✅ Авторизация успешно завершена![/bold green]\n\n"
                f"👤 Пользователь: [bold]{me.first_name} {me.last_name or ''}[/bold]\n"
                f"🏷️ Username: @{me.username or 'отсутствует'}\n"
                f"🆔 ID: {me.id}\n"
                f"💾 Сессия сохранена в: [cyan]{session_path}.session[/cyan]",
                title="Успех", border_style="green"
            ))
        else:
            console.print("[red]❌ Не удалось авторизовать аккаунт.[/red]")

    except Exception as e:
        console.print(f"[red]❌ Ошибка при авторизации: {e}[/red]")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(authenticate())
