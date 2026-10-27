"""Настройки бота. Все значения читаются из .env (см. .env.example)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip()

# Необязательно: путь к cookies.txt (YouTube, Instagram, TikTok и т.д.)
COOKIES_FILE: str | None = (os.getenv("COOKIES_FILE") or "").strip() or None

# Необязательно: содержимое cookies.txt целиком, если файл положить некуда
# (PaaS вроде Render: диск временный, зато есть переменные окружения).
# Переводы строк можно писать как \n — при старте они превратятся в реальные.
_YT_COOKIES = (os.getenv("YT_COOKIES") or "").strip()
if _YT_COOKIES and not COOKIES_FILE:
    try:
        _cookies_path = BASE_DIR / "cookies.txt"
        _cookies_path.write_text(
            _YT_COOKIES.replace("\\n", "\n").rstrip("\n") + "\n", encoding="utf-8"
        )
        COOKIES_FILE = str(_cookies_path)
    except OSError:
        pass

# Необязательно: какие клиенты YouTube пробовать, через запятую.
# Пример: YT_PLAYER_CLIENT=default,android — иногда обходит проверку «не бот».
YT_PLAYER_CLIENT: str = (os.getenv("YT_PLAYER_CLIENT") or "").strip()

# Необязательно: прокси для yt-dlp, например socks5://127.0.0.1:1080
PROXY: str | None = (os.getenv("PROXY") or "").strip() or None

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
