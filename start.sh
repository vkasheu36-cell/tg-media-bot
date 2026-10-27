#!/bin/sh
# Запуск бота в контейнере: сначала провайдер PO-токенов, потом сам бот.
set -eu

POT_URL="http://127.0.0.1:4416"

if [ "${ENABLE_POT:-1}" = "1" ] && [ -f /opt/pot/server/build/main.js ]; then
    node /opt/pot/server/build/main.js --port 4416 --host 127.0.0.1 &
    POT_PID=$!

    ok=0
    for _ in $(seq 1 30); do
        if node -e "fetch('$POT_URL/ping').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"; then
            ok=1
            break
        fi
        sleep 1
    done

    if [ "$ok" = "1" ]; then
        export POT_SERVER="$POT_URL"
        echo "[pot] провайдер PO-токенов запущен на $POT_URL"
    else
        echo "[pot] провайдер не поднялся — работаем без PO-токена"
        kill "$POT_PID" 2>/dev/null || true
    fi
fi

exec python bot.py
