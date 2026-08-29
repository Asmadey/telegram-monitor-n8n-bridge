import os
import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv

async def create_session():
    """
    Скрипт для первичной авторизации личного Telegram-аккаунта.
    Создает файл 'personal_account.session'.
    """
    print("--- Telegram User Session Creator ---")
    
    # Загружаем из .env или просим ввод
    load_dotenv()
    api_id = os.getenv('TELEGRAM_API_ID') or input("Введите API_ID: ")
    api_hash = os.getenv('TELEGRAM_API_HASH') or input("Введите API_HASH: ")
    phone = os.getenv('TELEGRAM_PHONE') or input("Введите номер телефона (+7...): ")

    client = TelegramClient('personal_account', int(api_id), api_hash)
    
    print(f"\nЗапуск клиента для номера {phone}...")
    await client.start(phone=phone)
    
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ Авторизация успешна!")
        print(f"Аккаунт: {me.first_name} (@{me.username or 'no_username'})")
        print(f"Файл сессии сохранен как: 'personal_account.session'")
        print("\nТеперь вы можете использовать этот файл для парсинга.")
    else:
        print("\n❌ Ошибка авторизации.")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(create_session())
