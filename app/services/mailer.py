"""Почта (транспорт — задача 2.9). Заглушка: письма складываются в reset_emails.

Красная фаза CDD: тест считает письма через этот список; в 2.9 список
заменит реальный транспорт (SMTP/сервис), а интерфейс останется.
"""
reset_emails: list[str] = []


async def send_password_reset_email(to_email: str, token: str) -> None:
    """Заглушка транспорта (задача 2.9). Токен в лог/список не пишем."""
    reset_emails.append(to_email)