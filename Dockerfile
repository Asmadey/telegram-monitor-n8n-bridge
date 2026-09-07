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
# Монолит server.py (~40 эндпоинтов без auth, К2) из образа не запускается
# никогда: даже случайный деплой не должен поднять незакрытую сборку.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
