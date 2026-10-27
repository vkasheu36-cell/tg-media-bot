# ---------- сборка провайдера PO-токенов (bgutil) ----------
# Нужен, чтобы YouTube на датацентровом IP (Render) не требовал «подтвердите вход».
FROM node:22-bookworm-slim AS pot

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates python3 make g++ pkg-config \
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch 2.0.2 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /pot

WORKDIR /pot/server
RUN npm ci && npx tsc && npm prune --omit=dev

# ---------- сам бот ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_ENV=production \
    PORT=8080

# ffmpeg — склейка видео и mp3; библиотеки ниже нужны Node-провайдеру (canvas)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
        libjpeg62-turbo libgif7 librsvg2-2 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# только бинарник node: npm в рантайме не нужен
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /pot /opt/pot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# start.sh поднимает провайдер токенов и запускает бота
ENV ENABLE_POT=1
EXPOSE 8080

CMD ["sh", "/app/start.sh"]
