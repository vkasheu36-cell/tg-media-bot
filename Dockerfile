# Образ для PaaS-хостингов (Render, Koyeb, ClawCloud, Bot-Hosting и т.п.)
# Локально: docker build -t tg-media-bot . && docker run -e BOT_TOKEN=... -p 8080:8080 tg-media-bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# ffmpeg нужен для склейки видео и конвертации в mp3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Без PUBLIC_URL бот работает через long polling, с ним — через вебхук
ENV PUBLIC_URL=""
EXPOSE 8080

CMD ["python", "bot.py"]
