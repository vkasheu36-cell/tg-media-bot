# Образ для PaaS-хостингов (Render, Koyeb и т.п.) и Docker на VPS.
# Локально: docker build -t tg-media-bot . && docker run -e BOT_TOKEN=... -p 8080:8080 tg-media-bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1

# Deno — JS-движок для yt-dlp: решает n-challenge YouTube. Без него YouTube
# с cookies отдаёт «Requested format is not available» и 0 форматов.
ARG DENO_VERSION=2.9.8

# ffmpeg нужен для склейки видео, mp3 и упаковки AAC с Zvyki в m4a
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
        -o /tmp/deno.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && rm -f /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version

# usque — клиент Cloudflare WARP по MASQUE поверх TCP/HTTP2.
# YouTube режет IP датацентров Render (429/403), а cookies не спасают.
# Обычный WireGuard здесь не годится: на PaaS закрыт исходящий UDP,
# а у usque есть режим --http2 (обычный TCP 443).
ARG USQUE_VERSION=4.2.1
ARG USQUE_SHA256=4117e20695078af9c11edecd1a826c009bbc7ea0b7f64458612b4198910bc313
RUN curl -fsSL "https://github.com/Diniboy1123/usque/releases/download/v${USQUE_VERSION}/usque_${USQUE_VERSION}_linux_amd64.zip" \
        -o /tmp/usque.zip \
    && echo "${USQUE_SHA256}  /tmp/usque.zip" | sha256sum -c - \
    && unzip -o /tmp/usque.zip -d /tmp/usque \
    && install -m 0755 /tmp/usque/usque /usr/local/bin/usque \
    && rm -rf /tmp/usque /tmp/usque.zip \
    && usque version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ранняя проверка: несовместимые файлы (например, старый inline.py рядом с
# новым downloader.py) валят сборку сразу, а не при запуске на Render.
# Заодно убеждаемся, что Deno и yt-dlp-ejs на месте — без них YouTube молчит.
RUN chmod +x start.sh \
    && python -c "import bot" \
    && python -c "import shutil, yt_dlp_ejs, sys; deno = shutil.which('deno'); print('deno:', deno); sys.exit(0 if deno else 'deno не найден')"

# Без PUBLIC_URL бот работает через long polling, с ним — через вебхук
ENV PUBLIC_URL=""
EXPOSE 8080

# start.sh поднимает WARP (если WARP=1) и передаёт управление боту
CMD ["sh", "start.sh"]
