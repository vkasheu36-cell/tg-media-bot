# Деплой на Render (бесплатно, без карты)

Бот уже умеет работать через вебхук: на Render он сам возьмёт адрес из
`RENDER_EXTERNAL_URL`, поднимет HTTP-сервер и поставит вебхук в Telegram.
Long polling на Render не годится — платформа ждёт открытый порт.

## 1. Выложить код на GitHub

1. Зарегистрируйтесь на https://github.com (если аккаунта нет — 2 минуты).
2. https://github.com/new → **Repository name**: `tg-media-bot` → **Public** → **Create repository**.
3. На странице пустого репозитория: **uploading an existing file**.
4. Перетащите в окно **все файлы из этой папки** (кроме этого `.md`), затем **Commit changes**.

Важно: файл `.env` загружать нельзя — там токен.

## 2. Создать сервис на Render

1. https://dashboard.render.com/register → **GitHub** → разрешить доступ к репозиторию.
2. **New +** → **Web Service** → выбрать `tg-media-bot` → **Connect**.
3. Настройки:
   - **Language / Runtime**: `Docker` (определится по `Dockerfile` сам);
   - **Instance Type**: `Free`;
   - **Health Check Path**: `/health`;
   - **Region**: ближайший к вам (например, Frankfurt).
4. **Environment Variables** → добавить:
   - `BOT_TOKEN` = токен от @BotFather;
   - `MAX_UPLOAD_MB` = `49` (необязательно);
   - `FA_COOKIE` = `a=…; b=…` для работ FurAffinity 18+ (необязательно);
   - `COBALT_API` = `https://ваш-cobalt|Api-Key …` для TikTok/Instagram/Reddit/Rutube/VK/Pinterest и других (необязательно);
   - `COBALT_YOUTUBE` = `1` — гнать YouTube через cobalt (по умолчанию `0`: yt-dlp + cookies);
   - `COBALT_SERVICES` = `all` — все сервисы cobalt, кроме YouTube (необязательно; `none` выключает);
   - `COOKIES_FILE` = `/etc/secrets/cookies.txt` (Secret File) — для YouTube и Instagram.
5. **Create Web Service** и подождать 3–5 минут сборки.

В образе уже есть **Cloudflare WARP** (`usque`): при старте `start.sh`
поднимает локальный SOCKS5 и отдаёт его только yt-dlp. Это нужно, потому что
YouTube блокирует датацентровые IP Render (HTTP 429/403). Отключается
переменной `WARP=0` (см. раздел 5).

> **Не вставляйте cookies в переменную `YT_COOKIES`.** Linux не принимает
> аргументы длиннее ~128 КБ, поэтому большой `cookies.txt` (150+ строк) валит
> сборку сообщением `exec /usr/local/bin/run-buildkit.sh: argument list too long`.
> Удалите `YT_COOKIES` из Environment и используйте Secret File (см. раздел 5).

В логах должно появиться:

```
Подключился как @Vif_media_music_bot
Вебхук установлен: https://tg-media-bot.onrender.com/webhook
HTTP-сервер слушает 0.0.0.0:10000
[warp] туннель поднят, YouTube пойдёт через socks5h://127.0.0.1:8086
Бот запущен. … youtube: yt-dlp, JS-движок: deno 2.9.8 …, прокси Telegram: нет, прокси YouTube: socks5h://127.0.0.1:8086
```

## 3. Не давать сервису засыпать

Бесплатный Render останавливает контейнер после 15 минут без входящих запросов.
Первый запрос после сна будит его 30–60 секунд — за это время Telegram успевает
отвалиться по таймауту, а inline-режим (у него лимит 10 с) не работает вообще.

Решение — бесплатный пингер:

1. https://cron-job.org → регистрация (без карты);
2. **Create cronjob**;
3. URL: `https://tg-media-bot.onrender.com/health`;
4. Расписание: каждые 10 минут;
5. Save.

Теперь контейнер не засыпает и бот отвечает сразу.

## 4. Выключить локальную копию

На компьютере бот работать не должен: один токен — один способ получения
обновлений. Если локальный бот останется на long polling, Telegram начнёт
конфликтовать с вебхуком (`409 Conflict`).

## 5. YouTube, Instagram, Reddit, Rutube

YouTube по умолчанию качается напрямую через yt-dlp с cookies из `COOKIES_FILE`.
Render — датацентр, и YouTube часто просит «Sign in to confirm you're not a bot» —
именно cookies решают эту проблему. Вернуть YouTube в cobalt можно переменной
`COBALT_YOUTUBE=1`, но тогда нужен инстанс, который умеет YouTube.

В образе уже есть **Deno** и пакет **yt-dlp-ejs**: yt-dlp решает на JavaScript
задачу n-challenge, без которой YouTube с cookies отдаёт «Requested format is
not available» и ни одного формата. Ничего доустанавливать на Render не нужно.

Если в логах «Failed to extract any player response» — бот сам перебирает
player_client и запоминает рабочий. Проверить, что доступно с IP Render:
`/yt_debug https://youtu.be/...`. Команда заодно покажет, подхватились ли cookies
(ключи входа), есть ли JS-движок и какие клиенты ответили.

### WARP: обход блокировки IP Render

Если `/yt_debug` показывает `watch: HTTP 429` и `вход по cookies: есть`,
значит Google блокирует не аккаунт, а сам IP датацентра. Cookies тут не помогут:
по документации yt-dlp 429/402 — это перерасход IP, лечится только другим
адресом. Поэтому в образ встроен `usque` — клиент Cloudflare WARP.

Почему именно он: обычный WireGuard/warp-plus требует исходящий UDP, а на PaaS
он закрыт. `usque` умеет MASQUE поверх обычного **TCP/HTTP2** (`--http2`) и
отдаёт локальный SOCKS5. `start.sh` при старте:

1. регистрирует бесплатное устройство WARP (если нет готового конфига);
2. запускает `usque socks --http2` на `127.0.0.1:8086`;
3. проверяет туннель через `curl`;
4. выставляет `YT_PROXY=socks5h://127.0.0.1:8086` и запускает бота.

Прокси получает только yt-dlp — Telegram и остальные сервисы идут напрямую
(поэтому в логе две строки: «прокси Telegram» и «прокси YouTube»).

Признаки работающего туннеля в `/yt_debug`:

```
YT_PROXY: есть
IP через прокси: 104.28.… | US | AS13335 Cloudflare, Inc.
страницы YouTube (через прокси): watch: HTTP 200, … playability OK
```

Если туннель не поднялся, бот всё равно запустится — просто без прокси
(в логе будет `[warp] туннель не поднялся`). Выключить WARP совсем: `WARP=0`.

Если Cloudflare ограничил регистрацию с IP Render, сделайте конфиг один раз
на своём компьютере:

```bash
usque register -a -c usque.json
```

Загрузите `usque.json` как **Secret File** и задайте
`USQUE_CONFIG=/etc/secrets/usque.json`. Файл содержит ключ устройства WARP —
не публикуйте его.

WARP бесплатный и общий: выходной IP тоже может быть помечен Google. Тогда
поможет другой выход (WARP+, ZeroTrust) или свой доверенный SOCKS5/HTTP-прокси
в `YT_PROXY`. Через случайные публичные прокси cookies пускать нельзя.

### Cookies через Secret File

1. Соберите файл из браузера, где вы вошли в YouTube (браузер лучше закрыть):

   ```bash
   python -m yt_dlp --cookies-from-browser chrome --cookies cookies.txt \
     --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
   ```

   `chrome` можно заменить на `edge`, `firefox`, `brave`, `opera`, `vivaldi`.
   Экспорт из Cookie-Editor теряет HttpOnly-пары (`SID`, `HSID`, `APISID`),
   поэтому он хуже.

2. Проверьте состав (значения не печатаются):

   ```bash
   python inspect_cookies.py cookies.txt
   ```

3. Render → ваш сервис → **Environment** → **Secret Files** → **Add Secret File**:
   - **Filename**: `cookies.txt`
   - **Contents**: весь файл.
4. В **Environment Variables**: `COOKIES_FILE` = `/etc/secrets/cookies.txt`.
   Переменную `YT_COOKIES` удалите.
5. **Manual Deploy → Clear build cache & deploy**, затем `/yt_debug <ссылка>`.

В отчёте должно быть `ключи входа: SID, HSID, SSID, APISID, SAPISID, LOGIN_INFO,
__Secure-1PSID, __Secure-3PSID`, строка с `deno`, `вход по cookies: есть`
и хотя бы один клиент `OK`.

Если `вход по cookies: НЕТ` — файл не рабочий (протух или экспортирован без
HttpOnly-пар). Пересоберите его через `yt_dlp --cookies-from-browser …` из
браузера, где вы реально вошли в YouTube, и загрузите заново.

Secret File на Render смонтирован только для чтения, а yt-dlp обновляет cookies
после каждого запроса. Бот сам копирует файл в `/tmp` при старте и работает с
копией, поэтому ошибки `Read-only file system: /etc/secrets/cookies.txt` быть
не должно. После замены Secret File нужен перезапуск сервиса.

Признак, что исправление на месте — строка в логе при старте:
`cookies: /tmp/cookies-runtime.txt (3xxx байт)`. Если там снова `/etc/secrets/...`,
значит задеплоена старая сборка.

Если после живых cookies всё равно «Sign in to confirm you're not a bot» —
значит Google блокирует сам IP Render. В образе для этого уже есть WARP
(см. раздел «WARP» выше); альтернатива — свой прокси в `YT_PROXY` или
PO-токены (провайдер `POT_SERVER`).

Cookies — это доступ к аккаунту: не публикуйте файл и не пересылайте его в чат.

TikTok/Instagram/Reddit/Rutube/VK/Pinterest и остальные сервисы: бот сначала
пробует `COBALT_API` (если задан), затем обычный yt-dlp или свой разбор.
Список сервисов — `COBALT_SERVICES` (по умолчанию `all`, кроме YouTube).

Важно: публичный `api.cobalt.tools` YouTube **не поддерживает**, а бесплатные
публичные инстансы требуют токен turnstile (`error.api.auth.jwt.missing`).
Instagram и Reddit на публичных инстансах часто отвечают «нужен вход».
Свой cobalt поднимается по инструкции imputnet/cobalt; для YouTube ему нужны
PO-токены (`YOUTUBE_SESSION_SERVER`) или cookies аккаунта, для Instagram/Reddit —
cookies этих сервисов в инстансе.

## Ограничения бесплатного тарифа

- 512 МБ RAM и 0.1 CPU: музыка (Hitmo и Zvyki отдают готовые файлы) летает,
  а вот конвертация видео через ffmpeg будет заметно медленной;
- 750 часов в месяц на все бесплатные сервисы аккаунта — одного бота
  (≈730 ч) это касается впритык;
- диск временный: после каждого деплоя кэш `file_id` обнуляется.

Если упрётесь в CPU — тот же репозиторий переезжает на VPS без изменений
(там нужен long polling: просто не задавайте `PUBLIC_URL`).
