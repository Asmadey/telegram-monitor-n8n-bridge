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

# Запуск приложения
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
