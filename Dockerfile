FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей при необходимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Установка Python зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование проекта
COPY . .

# Переменные окружения и порт (Railway пробрасывает $PORT)
ENV PORT=8000
EXPOSE 8000

# Запуск приложения — ТОЛЬКО новая сборка (закрыта по умолчанию, задача 2.3).
# Монолит удалён задачей 7.4 и запрещён к возвращению (test_75); CMD назван
# явно на случай, если кто-то попробует поднять из образа что-то другое.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
