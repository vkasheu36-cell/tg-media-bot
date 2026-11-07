#!/bin/sh
# Запуск бота в контейнере.
#
# Зачем нужен WARP: Render выдаёт датацентровый IP, а YouTube режет такие
# адреса (HTTP 429 на watch, 403 на player API) — cookies не помогают.
# usque поднимает Cloudflare WARP по MASQUE поверх обычного TCP/HTTP2
# (UDP на PaaS обычно закрыт) и отдаёт локальный SOCKS5. Прокси получает
# только yt-dlp через YT_PROXY; Telegram и остальные сервисы идут напрямую.
#
# Переключатели:
#   WARP=1        — включить туннель (по умолчанию)
#   WARP=0        — не запускать usque
#   USQUE_CONFIG  — готовый config.json (например, Render Secret File);
#                   если файла нет, устройство регистрируется при старте
#   WARP_PORT     — локальный порт SOCKS5 (по умолчанию 8086)
set -u

USQUE_BIN="${USQUE_BIN:-/usr/local/bin/usque}"
USQUE_CONFIG="${USQUE_CONFIG:-/tmp/usque.json}"
WARP="${WARP:-1}"
WARP_PORT="${WARP_PORT:-8086}"

warp_log() {
    echo "[warp] $*"
}

if [ "$WARP" = "1" ] && [ -x "$USQUE_BIN" ]; then
    if [ ! -s "$USQUE_CONFIG" ]; then
        warp_log "регистрирую устройство WARP…"
        attempt=1
        while [ "$attempt" -le 3 ]; do
            if "$USQUE_BIN" register -a -c "$USQUE_CONFIG" \
                >/tmp/usque-register.log 2>&1; then
                break
            fi
            warp_log "регистрация не удалась (попытка $attempt)"
            attempt=$((attempt + 1))
            sleep 5
        done
    fi

    if [ -s "$USQUE_CONFIG" ]; then
        # GOMEMLIMIT держит heap usque в узде: на Render всего 512 МБ,
        # а userspace-стек gVisor заметно прожорливее обычного клиента.
        GOMEMLIMIT="${WARP_MEMLIMIT:-180MiB}" \
            "$USQUE_BIN" socks -c "$USQUE_CONFIG" --http2 \
            -b 127.0.0.1 -p "$WARP_PORT" >/tmp/usque.log 2>&1 &
        usque_pid=$!

        ready=0
        attempt=1
        while [ "$attempt" -le 12 ]; do
            if ! kill -0 "$usque_pid" 2>/dev/null; then
                break
            fi
            if curl -fsS --max-time 3 \
                --socks5-hostname "127.0.0.1:$WARP_PORT" \
                https://www.cloudflare.com/cdn-cgi/trace >/dev/null 2>&1; then
                ready=1
                break
            fi
            attempt=$((attempt + 1))
            sleep 1
        done

        if [ "$ready" = "1" ]; then
            YT_PROXY="socks5h://127.0.0.1:$WARP_PORT"
            export YT_PROXY
            warp_log "туннель поднят, YouTube пойдёт через $YT_PROXY"
        else
            warp_log "туннель не поднялся — работаю без прокси"
            kill "$usque_pid" 2>/dev/null || true
        fi
    else
        warp_log "нет конфига WARP — работаю без прокси"
    fi
elif [ "$WARP" != "0" ]; then
    warp_log "usque не найден — работаю без прокси"
fi

exec python bot.py
