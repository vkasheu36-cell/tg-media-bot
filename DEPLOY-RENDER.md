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
   - `COBALT_API` = `https://ваш-cobalt|Api-Key …` для Instagram/Reddit/Rutube (необязательно);
   - `COBALT_YOUTUBE` = `1` — гнать YouTube через cobalt (по умолчанию `0`: yt-dlp + cookies);
   - `COBALT_SERVICES` = `instagram,reddit,rutube` (необязательно);
   - `COOKIES_FILE` = `/etc/secrets/cookies.txt` (Secret File) — для YouTube и Instagram.
5. **Create Web Service** и подождать 3–5 минут сборки.

> **Не вставляйте cookies в переменную `YT_COOKIES`.** Linux не принимает
> аргументы длиннее ~128 КБ, поэтому большой `cookies.txt` (150+ строк) валит
> сборку сообщением `exec /usr/local/bin/run-buildkit.sh: argument list too long`.
> Удалите `YT_COOKIES` из Environment и используйте Secret File (см. раздел 5).

В логах должно появиться:

```
Подключился как @Vif_media_music_bot
Вебхук установлен: https://tg-media-bot.onrender.com/webhook
HTTP-сервер слушает 0.0.0.0:10000
Бот запущен. … youtube: yt-dlp, JS-движок: deno 2.9.8 …, прокси: нет
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
__Secure-1PSID, __Secure-3PSID`, строка с `deno` и хотя бы один клиент `OK`.

Cookies — это доступ к аккаунту: не публикуйте файл и не пересылайте его в чат.

Instagram/Reddit/Rutube: бот сначала пробует `COBALT_API` (если задан), затем
обычный yt-dlp. Список сервисов — `COBALT_SERVICES` (по умолчанию
`instagram,reddit,rutube`).

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
