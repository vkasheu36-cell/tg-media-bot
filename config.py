"""Настройки бота. Все значения читаются из .env (см. .env.example)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip()

# Необязательно: путь к cookies.txt (YouTube, Instagram, TikTok и т.д.)
COOKIES_FILE: str | None = (os.getenv("COOKIES_FILE") or "").strip() or None


def writable_cookie_copy(path: str) -> str:
    """yt-dlp перезаписывает cookie-файл после запроса, а Secret File на Render
    доступен только на чтение (/etc/secrets). Делаем рабочую копию в /tmp."""
    source = Path(path)
    if not source.exists() or os.access(source, os.W_OK):
        return path
    target = Path(tempfile.gettempdir()) / "cookies-runtime.txt"
    try:
        target.write_bytes(source.read_bytes())
    except OSError:
        return path
    return str(target)


if COOKIES_FILE:
    COOKIES_FILE = writable_cookie_copy(COOKIES_FILE)

# Необязательно: содержимое cookies.txt целиком, если файл положить некуда
# (PaaS вроде Render: диск временный, зато есть переменные окружения).
# Переводы строк можно писать как \n — при старте они превратятся в реальные.
_YT_COOKIES = (os.getenv("YT_COOKIES") or "").strip()
if _YT_COOKIES and not COOKIES_FILE:
    try:
        _cookies_path = BASE_DIR / "cookies.txt"
        # Render может прислать \r\n; cookie-парсер ждёт обычные \n и табы.
        _content = _YT_COOKIES.replace("\r\n", "\n").replace("\r", "\n")
        _cookies_path.write_text(
            _content.replace("\\n", "\n").rstrip("\n") + "\n", encoding="utf-8"
        )
        COOKIES_FILE = str(_cookies_path)
    except OSError:
        pass

# Необязательно: какие клиенты YouTube пробовать, через запятую.
# Пример: YT_PLAYER_CLIENT=default,android — иногда обходит проверку «не бот».
YT_PLAYER_CLIENT: str = (os.getenv("YT_PLAYER_CLIENT") or "").strip()

# Необязательно: адрес локального провайдера PO-токенов (bgutil).
# Нужен на датацентровых IP, где YouTube требует подтверждение «я не робот».
# В контейнере выставляется автоматически из start.sh.
POT_SERVER: str = (os.getenv("POT_SERVER") or "").strip()

# Необязательно: прокси для yt-dlp, например socks5://127.0.0.1:1080
PROXY: str | None = (os.getenv("PROXY") or "").strip() or None

# --- cobalt: внешний API для YouTube (опционально) и других сервисов --------
# Так же, как cobold (tskau/cobold), бот отправляет ссылку в cobalt и забирает
# готовый файл. Инстансы перечисляются через запятую или с новой строки:
#   https://cobalt.example.com
#   https://cobalt.example.com|Api-Key 12345678
#   https://cobalt.example.com|Bearer eyJhbGciOi...
# Всё после | уходит в заголовок Authorization.
# Публичный api.cobalt.tools YouTube не поддерживает, а бесплатные инстансы
# требуют токен — нужен свой инстанс или ключ от его владельца.
COBALT_API: str = (os.getenv("COBALT_API") or "").strip()

# Необязательно: гнать YouTube через cobalt. По умолчанию выключено —
# YouTube идёт напрямую в yt-dlp с cookies из COOKIES_FILE: это быстрее и
# не тратит время на cobalt, который часто не умеет YouTube без PO-токенов.
COBALT_YOUTUBE: bool = (
    os.getenv("COBALT_YOUTUBE") or ""
).strip().lower() in {"1", "true", "yes", "on"}

# Необязательно: стартовое качество при скачивании через cobalt (1080/720/480/360/max).
# Если файл не влезает в лимит Telegram, бот сам понижает качество.
COBALT_QUALITY: str = (os.getenv("COBALT_QUALITY") or "1080").strip()

# Необязательно: какие сервисы сначала отдавать в cobalt (через запятую).
# YouTube сюда не входит — для него отдельный переключатель COBALT_YOUTUBE.
# Остальные сервисы качаются прежним путём (yt-dlp и свои разборы).
COBALT_SERVICES: str = (
    os.getenv("COBALT_SERVICES") or "instagram,reddit,rutube"
).strip()

# Необязательно: прокси только для запросов в cobalt (не для Telegram).
COBALT_PROXY: str | None = (os.getenv("COBALT_PROXY") or "").strip() or None

# Cookies FurAffinity для работ 18+ (аккаунт second-acc).
# FA отдаёт «взрослые» страницы только после входа, а вход по паролю с
# серверного IP отвечает 503 — поэтому используются готовые cookies сессии.
# Если задан FA_COOKIE в окружении — он важнее встроенного значения.
# Если сессия протухла: войти в FA в браузере и обновить a и b.
FA_COOKIE: str = (
    os.getenv("FA_COOKIE")
    or "a=d4a2cf01-652b-47c7-88fa-95a1285534fd; b=d32858cd-9796-4293-91da-7a2e100922ef"
).strip()

# Необязательно: аккаунт FurAffinity, под которым бот входит сам.
# Нужен только для работ 18+ (без входа FA отдаёт страницу без картинки).
FA_LOGIN: str = (os.getenv("FA_LOGIN") or "second-acc").strip()
FA_PASSWORD: str = (os.getenv("FA_PASSWORD") or "12983476kA!").strip()

# Необязательно: прокси для Telegram API (если api.telegram.org заблокирован).
# Если не задан, используется PROXY.
TELEGRAM_PROXY: str | None = (
    (os.getenv("TELEGRAM_PROXY") or os.getenv("PROXY") or "").strip() or None
)

# Необязательно: публичный https-адрес бота (например, https://bot.example.app).
# Если задан — бот работает через вебхук, а не через long polling.
# Так работают почти все PaaS: Render, Bot-Hosting, Koyeb, ClawCloud и т.п.
# На Render адрес подставляется автоматически (переменная RENDER_EXTERNAL_URL),
# поэтому вручную PUBLIC_URL там указывать не нужно.
# Если пусто — обычный long polling (VPS, домашний ПК).
PUBLIC_URL: str = (
    (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "")
    .strip()
    .rstrip("/")
)

# Порт HTTP-сервера в режиме вебхука. PaaS обычно сам подставляет свой в PORT.
PORT: int = int(os.getenv("PORT") or "8080")

# Необязательно: секрет для заголовка X-Telegram-Bot-Api-Secret-Token.
# Защищает эндпоинт от левых запросов. Если пусто — выключено.
WEBHOOK_SECRET: str = (os.getenv("WEBHOOK_SECRET") or "").strip()

# Куда складывать временные файлы
DOWNLOADS_DIR: Path = Path(os.getenv("DOWNLOADS_DIR") or (BASE_DIR / "downloads"))

# Лимит обычного Bot API — 50 МБ. Держим запас.
MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB") or "49")


def _find_ffmpeg_dir() -> str | None:
    """ffmpeg в PATH или в типовых местах установки (winget, chocolatey, C:\\ffmpeg)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
        Path("C:/ffmpeg"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg",
        Path("/usr/bin"),
        Path("/usr/local/bin"),
    ]
    for root in roots:
        if not root.exists():
            continue
        for match in root.glob("**/bin/ffmpeg.exe"):
            return str(match.parent)
        if (root / "ffmpeg").exists():
            return str(root)

    return None


FFMPEG_DIR: str | None = _find_ffmpeg_dir()

# Чтобы yt-dlp и postprocessor'ы нашли ffmpeg, даже если PATH не обновился
if FFMPEG_DIR and FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
