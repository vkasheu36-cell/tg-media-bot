"""Вся работа с yt-dlp: поиск треков, скачивание аудио и медиа по ссылке."""
from __future__ import annotations

import asyncio
import hashlib
import html
import http.cookiejar
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yt_dlp

from config import (
    COBALT_API,
    COBALT_PROXY,
    COBALT_QUALITY,
    COBALT_SERVICES,
    COBALT_YOUTUBE,
    COOKIES_FILE,
    FA_COOKIE,
    FA_LOGIN,
    FA_PASSWORD,
    FFMPEG_DIR,
    MAX_UPLOAD_MB,
    POT_SERVER,
    PROXY,
    YT_PLAYER_CLIENT,
    YT_PROXY,
    writable_cookie_copy,
)

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}

# yt-dlp качает из X только видео, картинки в постах он не умеет —
# для них есть отдельный путь через публичный API fxtwitter/vxtwitter
X_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/status/(\d+)"
)

# общий лимит одновременных скачиваний (и в личке, и в inline)
SEMAPHORE = asyncio.Semaphore(3)

# Hitmo (ru.hitmoz.org): русская музыка, отдаёт готовые mp3 без yt-dlp.
# Сайт стоит за простым WAF: без браузерных заголовков отвечает 403.
HITMO_BASE = "https://ru.hitmoz.org"
HITMO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Upgrade-Insecure-Requests": "1",
    # WAF Hitmo пускает только запросы, похожие на переход из браузера
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_HITMO_ITEM_RE = re.compile(r"data-musmeta='(\{.*?\})'", re.S)
_HITMO_TIME_RE = re.compile(r'track__fulltime">\s*(\d+):(\d{2})')
_HITMO_SONG_RE = re.compile(r"hitmoz\.org/song/(\d+)")
_HITMO_MP3_RE = re.compile(r"\.mp3(?:$|\?)", re.I)

# Zvyki (zvyki.com): второй русский источник. Отдаёт AAC с прямыми ссылками
# в атрибуте data-mp3, поэтому тоже обходится без yt-dlp.
ZVYKI_BASE = "https://zvyki.com"
ZVYKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
_ZV_TRACK_RE = re.compile(r'<li class="track"(.*?)</li>', re.S)
_ZV_MP3_RE = re.compile(r'data-mp3="([^"]+)"')
_ZV_ID_RE = re.compile(r'data-id="(\d+)"')
_ZV_DUR_RE = re.compile(r'data-duration="(\d+)"')
_ZV_ARTIST_RE = re.compile(r'<span class="cartist">(.*?)</span>', re.S)
_ZV_TITLE_RE = re.compile(r'<span class="ctitle">(.*?)</span>', re.S)
_ZV_SONG_ID_RE = re.compile(r"zvyki\.com/song/(\d+)")


class DownloadError(Exception):
    """Ошибка, текст которой не стыдно показать пользователю."""


class DownloadCancelled(Exception):
    """Пользователь нажал «Остановить» — прерываем скачивание."""


@dataclass(slots=True)
class Track:
    provider: str  # "hm" (Hitmo), "yt" (YouTube) или "sc" (SoundCloud)
    video_id: str
    url: str
    title: str
    uploader: str
    duration: int


def _yt_proxy() -> str | None:
    """Прокси для YouTube: отдельный YT_PROXY важнее общего PROXY."""
    return YT_PROXY or PROXY


def _opts() -> dict:
    """Базовые опции yt-dlp (общие для всех запросов)."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "cachedir": False,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = writable_cookie_copy(COOKIES_FILE)
    proxy = _yt_proxy()
    if proxy:
        opts["proxy"] = proxy
    extractor_args: dict[str, dict[str, list[str]]] = {}
    # С cookies клиенты по умолчанию упираются в PO-токен, а web_embedded
    # отдаёт полный набор форматов — поэтому с авторизацией он идёт первым.
    clients = _yt_client or YT_PLAYER_CLIENT or ("web_embedded" if COOKIES_FILE else "")
    if clients:
        extractor_args["youtube"] = {
            "player_client": [c.strip() for c in clients.split(",") if c.strip()]
        }
    if POT_SERVER:
        # локальный провайдер PO-токенов: YouTube считает запрос «браузерным»
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_SERVER]}
    if extractor_args:
        opts["extractor_args"] = extractor_args
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


# YouTube на датацентровом IP часто не отдаёт player response для клиента по
# умолчанию. Перебираем клиентов, пока какой-нибудь не ответит, и запоминаем
# рабочий на процесс — чтобы следующие ссылки качались сразу через него.
_YT_CLIENT_LADDER = (
    # web_embedded отдаёт полный набор форматов и с cookies, и без них;
    # visionos хорош без cookies; tv сейчас почти всегда требует PO-токен.
    "web_embedded",
    "tv_samsung",
    "visionos",
    "android_vr",
    "mweb",
    "tv_simply",
    "tv",
    "web_safari",
    "ios",
    "web",
)
_yt_client: str | None = None


def _yt_client_opts(client: str) -> dict:
    """Опции yt-dlp с принудительным player_client для YouTube."""
    opts = _opts()
    args = dict(opts.get("extractor_args") or {})
    args["youtube"] = {"player_client": [client]}
    opts["extractor_args"] = args
    return opts


def _yt_player_blocked(exc: Exception) -> bool:
    """Похоже ли, что YouTube не отдал плеер (а не «видео удалено»)."""
    low = str(exc).lower()
    return (
        "failed to extract any player response" in low
        or "sign in to confirm" in low
        or "not a bot" in low
        or "no video formats" in low
        or "the page needs to be reloaded" in low
    )


def _yt_should_retry(exc: Exception, download: bool) -> bool:
    """Стоит ли перебирать player_client после этой ошибки."""
    if _yt_player_blocked(exc):
        return True
    # при пробе форматов не запрашивается, поэтому такая ошибка тоже означает,
    # что клиент не отдал ролик (а не что выбранного качества нет)
    return not download and "requested format is not available" in str(exc).lower()


def _extract_info(url: str, opts: dict, download: bool = False):
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=download)
        return ydl.sanitize_info(data) if data else {}


def _extract_with_rescue(
    url: str, opts: dict, download: bool = False, cancel=None
):
    """extract_info; если YouTube не дал плеер — перебираем player_client."""
    global _yt_client

    try:
        return _dns_retry(lambda: _extract_info(url, opts, download=download))
    except Exception as exc:  # noqa: BLE001
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled() from exc
        if isinstance(exc, DownloadCancelled):
            raise
        if not is_youtube(url) or not _yt_should_retry(exc, download):
            raise

        # Битые или неполные cookies хуже, чем их отсутствие: YouTube видит
        # «полувход» и требует то, чего у yt-dlp нет. Пробуем тот же запрос без
        # cookies — если получилось, видео скачается, а не упадёт с ошибкой.
        if opts.get("cookiefile"):
            plain = dict(opts)
            plain.pop("cookiefile", None)
            plain["extractor_retries"] = 1
            plain["retries"] = 1
            plain["socket_timeout"] = 12
            try:
                info = _dns_retry(lambda: _extract_info(url, plain, download=download))
            except Exception as inner:  # noqa: BLE001
                log.info("YouTube: без cookies тоже не вышло: %s", inner)
            else:
                log.warning("YouTube: с cookies не получилось, качаю без них")
                return info

        # клиенты из YT_PLAYER_CLIENT уже пробовались вместе — не повторяем их
        explicit = {c.strip() for c in (YT_PLAYER_CLIENT or "").split(",") if c.strip()}
        ladder: list[str] = [c for c in _YT_CLIENT_LADDER if c not in explicit]

        last = exc
        for client in ladder:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            attempt = dict(opts)
            # при переборе не тратим минуты на повторы одного и того же клиента
            attempt["extractor_retries"] = 1
            attempt["retries"] = 1
            attempt["socket_timeout"] = 12
            args = dict(attempt.get("extractor_args") or {})
            args["youtube"] = {"player_client": [client]}
            attempt["extractor_args"] = args
            try:
                info = _dns_retry(lambda: _extract_info(url, attempt, download=download))
            except Exception as inner:  # noqa: BLE001
                last = inner
                log.info("YouTube: клиент %s не подошёл: %s", client, inner)
                continue
            _yt_client = client
            log.warning("YouTube: работает player_client=%s", client)
            return info
        if _yt_player_blocked(exc) and not _yt_player_blocked(last):
            raise exc
        raise last


_SERVICES = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "tt.site": "TikTok",
    "instagram.com": "Instagram",
    "twitter.com": "X",
    "x.com": "X",
    "vk.com": "VK",
    "vk.ru": "VK",
    "vkvideo.ru": "VK",
    "vk.cc": "VK",
    "vk.link": "VK",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "vimeo.com": "Vimeo",
    "soundcloud.com": "SoundCloud",
    "hitmoz.org": "Hitmo",
    "zvyki.com": "Zvyki",
    "furaffinity.net": "FurAffinity",
    "facdn.net": "FurAffinity",
    "deliciousoranges.com": "Hitmo",
    "spotify.com": "Spotify",
    "music.apple.com": "Apple Music",
    "music.yandex.ru": "Яндекс.Музыка",
    "music.yandex.com": "Яндекс.Музыка",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "twitch.tv": "Twitch",
    "dailymotion.com": "Dailymotion",
    "rutube.ru": "Rutube",
    "ok.ru": "OK",
    "boosty.to": "Boosty",
}


_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})"
)

# Ссылки на стриминги, которые сами не отдают файл, но по ним можно найти трек
SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]+)"
)
SPOTIFY_ALBUM_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?album/([A-Za-z0-9]+)"
)
APPLE_MUSIC_RE = re.compile(
    r"https?://music\.apple\.com/([a-z]{2})/(album|song)/[^/?#]+/(\d+)"
    r"(?:\?[^#]*\bi=(\d+))?"
)
YANDEX_MUSIC_RE = re.compile(
    r"https?://music\.yandex\.[a-z]+/(?:album/\d+/)?track/(\d+)"
)
YANDEX_ALBUM_RE = re.compile(r"https?://music\.yandex\.[a-z]+/album/(\d+)")
_OG_RE = re.compile(
    r'<meta\s+property="og:(title|description)"\s+content="([^"]*)"', re.I
)


def _public_url(url: str) -> str:
    """Приводим ссылки YouTube к youtu.be.

    Голый youtube.com в этой сети отвечает NXDOMAIN, а www.youtube.com
    резолвится через раз — youtu.be работает стабильнее всего.
    """
    host = (urlsplit(url).netloc or "").lower()
    if host == "youtu.be" or host.endswith("youtube.com"):
        match = _YT_ID_RE.search(url)
        if match:
            return f"https://youtu.be/{match.group(1)}"
    return url


def _is_vk(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return (
        host in {"vk.com", "vk.ru", "vkvideo.ru", "vk.cc", "vk.link", "vk.me"}
        or host.endswith(".vk.com")
        or host.endswith(".vk.ru")
    )


def _vk_url(url: str) -> str:
    """VK переехал на vk.ru, но yt-dlp посты на стене понимает только на vk.com.

    Короткие vk.cc / vk.link раскрываем через редирект, домен приводим к vk.com.
    """
    host = (urlsplit(url).netloc or "").lower()
    bare = host[4:] if host.startswith("www.") else host

    if bare in {"vk.cc", "vk.link", "vk.me"}:
        request = urllib.request.Request(url, headers=HITMO_HEADERS)
        with urllib.request.urlopen(request, timeout=15) as response:
            url = response.geturl()

    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    if host.endswith("vk.ru"):
        host = host[: -len("vk.ru")] + "vk.com"
    return urlunsplit((parts.scheme or "https", host, parts.path, parts.query, parts.fragment))


VK_META_UA = "TelegramBot (like TwitterBot)"
# заглушка VK для постов без вложения: 1920x1010 «Пост из ленты ВКонтакте»
VK_STUB_IMAGE = "fafvjSB8ha2EWPn-VR6LIahBtBkf50LZSLbKfQ/BiN2pjgeJTw.png"
_VK_VIDEO_PATH_RE = re.compile(r"/(?:video|clip)-?\d", re.I)


def _meta_value(body: str, key: str) -> str:
    """og-тег по имени: порядок атрибутов и content/value бывают разными."""
    for tag in re.findall(r"<meta\b[^>]*>", body, re.I):
        if not re.search(
            rf'\b(?:property|name)\s*=\s*["\']{re.escape(key)}["\']', tag, re.I
        ):
            continue
        found = re.search(r'\b(?:content|value)\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if found:
            return html.unescape(found.group(1)).strip()
    return ""


def _vk_og_media(url: str) -> tuple[str, str] | None:
    """Что прикреплено к странице VK: ("photo"|"video"|"embed", url).

    Обычному браузеру VK отдаёт пустую JS-страницу (а на фото — редирект на
    badbrowser.php), зато боту предпросмотра отдаёт готовые og:image/og:video.
    Так разбираются ссылки на фото и посты, которые yt-dlp не понимает.
    """
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": VK_META_UA,
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("VK: OG-страница не открылась: %s", exc)
        return None

    video = (
        _meta_value(body, "og:video:secure_url")
        or _meta_value(body, "og:video:url")
        or _meta_value(body, "og:video")
    )
    if video:
        if "video_ext.php" in video:
            return "embed", video
        if urlsplit(video).path.lower().endswith((".mp4", ".m4v", ".mov", ".webm")):
            return "video", video
        # og:video вида vkvideo.ru/video… — это страница, её и так берёт yt-dlp
        return None

    image = _meta_value(body, "og:image:secure_url") or _meta_value(body, "og:image")
    if image.startswith("//"):
        image = "https:" + image
    if not image.startswith("http"):
        return None
    if _VK_VIDEO_PATH_RE.search(urlsplit(url).path):
        return None  # у страницы видео фото-вложения нет
    if VK_STUB_IMAGE in image:
        return None  # заглушка «Пост из ленты ВКонтакте», а не вложение
    if not _meta_value(body, "og:url"):
        # у постов без вложения VK отдаёт только title/description/заглушку
        return None
    return "photo", image


def _vk_download_direct(kind: str, media_url: str, out_dir: Path) -> tuple[Path, dict]:
    """Фото/видео VK качаем напрямую по ссылке из OG-тегов."""
    log.info("VK: качаю %s напрямую: %s", kind, media_url)
    data = _http_get(media_url, timeout=120)
    ext = _guess_ext(media_url)
    if kind == "photo":
        if ext not in {".jpg", ".png", ".webp", ".gif"}:
            ext = ".jpg"
    elif ext not in {".mp4", ".m4v", ".mov", ".webm"}:
        ext = ".mp4"
    path = out_dir / f"vk{ext}"
    path.write_bytes(data)
    return path, {"id": "", "title": "", "duration": None}


# ---------------------------------------------------------------- Reddit
# Reddit закрыл анонимный JSON (403 Blocked) и требует вход для yt-dlp, но
# публичный Atom-фид поста отдаёт и заголовок, и ссылки на медиа. Этого хватает:
# картинки лежат на i.redd.it, родные видео — на v.redd.it (DASH без входа).

_REDDIT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept": (
        "application/atom+xml,application/xml;q=0.9,"
        "text/html;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_REDDIT_COMMENTS_RE = re.compile(r"/comments/([A-Za-z0-9]+)", re.I)
_REDDIT_GALLERY_RE = re.compile(r"/gallery/([A-Za-z0-9]+)", re.I)
_REDDIT_VIDEO_RE = re.compile(r"https?://v\.redd\.it/([A-Za-z0-9]+)", re.I)
_REDDIT_IMAGE_RE = re.compile(
    r"https?://(?P<host>i\.redd\.it|preview\.redd\.it|i\.imgur\.com)/"
    r"(?P<file>[^\s\"'<>?]+)",
    re.I,
)
_REDDIT_YT_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?[^\s\"'<>]*v=|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.I,
)
_REDDIT_ATOM = {
    "a": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}


def _is_reddit(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    if host.startswith(("www.", "m.", "old.", "mobile.")):
        host = host.split(".", 1)[1]
    return host in {"reddit.com", "redd.it", "redditmedia.com", "redditstatic.com"}


def _reddit_get(url: str, timeout: int = 20) -> tuple[str, bytes]:
    """GET Reddit с запасным путём через WARP: у direct и туннеля разные лимиты."""
    request = urllib.request.Request(url, headers=_REDDIT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.geturl(), response.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 429} or not _yt_proxy():
            raise
        log.info("Reddit: direct HTTP %s, пробую через прокси", exc.code)
        status, body = _fetch_via_proxy(url, timeout=timeout)
        if status == 200:
            return url, body
        raise
    except Exception:
        if not _yt_proxy():
            raise
        log.info("Reddit: direct не ответил, пробую через прокси")
        status, body = _fetch_via_proxy(url, timeout=timeout)
        if status == 200:
            return url, body
        raise


def _reddit_canonical(url: str) -> str:
    """Раскрывает redd.it и /s/<token> до полной ссылки на пост."""
    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    path = parts.path or "/"

    if host.endswith("redd.it") and not host.startswith("v."):
        post_id = path.strip("/").split("/", 1)[0]
        if post_id:
            return f"https://www.reddit.com/comments/{post_id}"

    if _REDDIT_COMMENTS_RE.search(path):
        return urlunsplit(("https", "www.reddit.com", path, "", ""))

    if "/s/" in path:
        try:
            final, _ = _reddit_get(url, timeout=15)
        except urllib.error.HTTPError as exc:
            raise DownloadError(
                "Reddit: короткую ссылку /s/… не удалось раскрыть "
                f"(HTTP {exc.code}). Пришлите полную ссылку из адресной строки."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DownloadError(
                "Reddit: короткую ссылку /s/… не удалось раскрыть. "
                "Пришлите полную ссылку на пост."
            ) from exc
        if _REDDIT_COMMENTS_RE.search(urlsplit(final).path):
            return urlunsplit(("https", "www.reddit.com", urlsplit(final).path, "", ""))
        raise DownloadError(
            "Reddit: короткая ссылка /s/… уже не ведёт на пост (Reddit отправил "
            "на главную сабреддита). Такие ссылки истекают — пришлите полную "
            "ссылку из адресной строки вида reddit.com/r/…/comments/…"
        )

    match = _REDDIT_GALLERY_RE.search(path)
    if match:
        return f"https://www.reddit.com/comments/{match.group(1)}"

    raise DownloadError(
        "Reddit: не понял ссылку. Пришлите ссылку на пост "
        "(reddit.com/r/…/comments/…) или на файл i.redd.it / v.redd.it."
    )


def _reddit_feed_url(canonical: str) -> str:
    path = urlsplit(canonical).path.rstrip("/")
    return f"https://www.reddit.com{path}/.rss?limit=1"


def _reddit_feed_post(body: bytes) -> dict:
    """Первый entry фида — сам пост; остальные — комментарии."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise DownloadError("Reddit: фид поста пришёл битым.") from exc
    entries = root.findall("a:entry", _REDDIT_ATOM)
    if not entries:
        raise DownloadError("Reddit: пост не найден или удалён.")
    entry = entries[0]
    title_node = entry.find("a:title", _REDDIT_ATOM)
    title = "".join(title_node.itertext()).strip() if title_node is not None else ""
    link_node = entry.find("a:link", _REDDIT_ATOM)
    link = link_node.get("href") if link_node is not None else ""
    content_node = entry.find("a:content", _REDDIT_ATOM)
    content = (
        "".join(content_node.itertext()) if content_node is not None else ""
    )
    return {
        "title": html.unescape(title) or "Reddit",
        "link": link,
        "content": html.unescape(content),
    }


def _reddit_media(content: str) -> tuple[str, str] | None:
    """Возвращает («video»|«image»|«url», ссылка) для первого вложения поста."""
    video = _REDDIT_VIDEO_RE.search(content)
    if video:
        # DASH-манифест открыт без входа, в отличие от страницы поста.
        return "video", f"https://v.redd.it/{video.group(1)}/DASHPlaylist.mpd"

    youtube = _REDDIT_YT_RE.search(content)
    if youtube:
        return "url", f"https://youtu.be/{youtube.group(1)}"

    images = list(_REDDIT_IMAGE_RE.finditer(content))
    if images:
        # i.redd.it — полный файл; preview.* — тот же файл в уменьшенном виде.
        for match in images:
            host = match.group("host").lower()
            if host == "i.redd.it":
                return "image", f"https://i.redd.it/{match.group('file')}"
        for match in images:
            host = match.group("host").lower()
            if host == "preview.redd.it":
                return "image", f"https://i.redd.it/{match.group('file')}"
            if host == "i.imgur.com":
                return "image", f"https://i.imgur.com/{match.group('file')}"

    # Внешняя ссылка: берём её же, если это не служебный reddit-домен.
    for candidate in re.findall(r"https?://[^\s\"'<>]+", content):
        host = (urlsplit(candidate).netloc or "").lower()
        if host and not host.endswith(("reddit.com", "redd.it")):
            if any(
                token in host for token in ("imgur.com", "giphy.com", "redgifs.com")
            ):
                return "url", candidate
    return None


def _reddit_author(content: str) -> str:
    match = re.search(r"reddit\.com/user/([^\"'/<>\s]+)", content)
    return f"u/{match.group(1)}" if match else ""


def _reddit_download_sync(
    url: str,
    out_dir: Path,
    mode: str = "auto",
    on_progress=None,
    cancel: threading.Event | None = None,
) -> tuple[Path, dict]:
    """Reddit без входа: фид поста → картинка или DASH-манифест видео."""
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()

    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = _reddit_canonical(url)

    try:
        _, body = _reddit_get(_reddit_feed_url(canonical), timeout=25)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise DownloadError(
                "Reddit: временно ограничил запросы (HTTP 429). "
                "Подождите минуту и пришлите ссылку ещё раз."
            ) from exc
        if exc.code in {403, 401}:
            raise DownloadError(
                "Reddit: заблокировал серверный IP. Нужны cookies аккаунта "
                "(REDDIT_COOKIE) или свой прокси."
            ) from exc
        raise DownloadError(f"Reddit: фид поста ответил HTTP {exc.code}.") from exc
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"Reddit: не удалось открыть пост ({str(exc)[:120]}).") from exc

    post = _reddit_feed_post(body)
    media = _reddit_media(post["content"])
    if not media:
        raise DownloadError(
            "Reddit: в посте нет видео или картинки — бывает у текстовых постов. "
            "Пришлите ссылку на пост с вложением."
        )
    kind, media_url = media
    if kind == "image" and mode == "audio":
        raise DownloadError(
            "Reddit: в посте картинка, звуковой дорожки нет. "
            "Выберите «Фото-видео» — пришлю изображение."
        )
    info = {
        "title": post["title"],
        "uploader": _reddit_author(post["content"]),
        "duration": 0,
        "webpage_url": canonical,
    }

    if kind == "image":
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        try:
            data = _http_get(media_url, timeout=90)
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"Reddit: картинка не скачалась (HTTP {exc.code}).") from exc
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        ext = _guess_ext(media_url)
        if ext not in {".jpg", ".png", ".webp", ".gif"}:
            ext = ".jpg"
        path = out_dir / f"reddit{ext}"
        path.write_bytes(data)
        log.info("Reddit: картинка %s (%s байт)", media_url, len(data))
        return path, info

    if kind == "url":
        # YouTube/Imgur/RedGifs отдаём обычному пути yt-dlp ниже.
        raise _RedditExternal(media_url)

    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()

    opts: dict = _opts() | {
        "noplaylist": True,
        "outtmpl": str(out_dir / "%(title).60s [%(id)s].%(ext)s"),
        "progress_hooks": _progress_hook(on_progress, None, cancel),
    }
    if mode == "audio":
        opts |= {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    else:
        opts |= {
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        }

    try:
        _extract_with_rescue(media_url, opts, download=True, cancel=cancel)
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"Reddit: видео не скачалось ({str(exc)[:120]}).") from exc

    files = [
        p
        for p in out_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not files:
        raise DownloadError("Reddit: видео скачалось, но файл не найден.")
    path = max(files, key=lambda p: p.stat().st_size)
    info["duration"] = int(info.get("duration") or 0)
    log.info("Reddit: видео %s → %s", media_url, path.name)
    return path, info


class _RedditExternal(Exception):
    """В посте внешняя ссылка (YouTube и т.п.) — её обработает yt-dlp."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


# ---------------------------------------------------------------- Pinterest
# Публичная страница пина отдаётся без входа, но собирается React'ом: og-тегов
# почти нет. Зато в HTML есть <link id="pin-image-preload"> и Relay-данные с
# ссылками на оригинал/видео. Короткие pin.it раскрываем вручную: истёкшие
# ведут на главную, и тогда честно просим полную ссылку /pin/<id>/.

_PINTEREST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
_PINTEREST_PIN_RE = re.compile(r"/pin/(\d+)", re.I)
_PINTEREST_VIDEO_RE = re.compile(
    r"https?://[A-Za-z0-9.-]*pinimg\.com/videos/[^\"'\s<>\\]+?\.mp4[^\"'\s<>\\]*",
    re.I,
)
_PINTEREST_HLS_RE = re.compile(
    r"https?://[A-Za-z0-9.-]*pinimg\.com/videos/[^\"'\s<>\\]+?\.m3u8[^\"'\s<>\\]*",
    re.I,
)
_PINTEREST_ORIGINAL_RE = re.compile(
    r"https?://i\.pinimg\.com/originals/[^\"'\s<>\\]+", re.I
)
_PINTEREST_IMAGE_RE = re.compile(
    r"https?://i\.pinimg\.com/(?:originals|736x|564x|474x|236x)/[^\"'\s<>\\]+",
    re.I,
)


class _PinterestNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _is_pinterest(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    if host.startswith(("www.", "m.", "ru.", "mobile.")):
        host = host.split(".", 1)[1]
    return host == "pin.it" or host.endswith("pinterest.com") or host.startswith(
        "pinterest."
    )


def _pinterest_location(url: str) -> str:
    """Один шаг редиректа: возвращает Location, не скачивая тело."""
    opener = urllib.request.build_opener(_PinterestNoRedirect)
    request = urllib.request.Request(url, headers=_PINTEREST_HEADERS)
    try:
        with opener.open(request, timeout=20) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") or exc.headers.get("location")
        if location:
            return urllib.parse.urljoin(url, location)
        raise


def _pinterest_canonical(url: str) -> str:
    """Раскрывает pin.it и приводит ссылку к /pin/<id>/."""
    current = url
    for _ in range(6):
        parts = urlsplit(current)
        match = _PINTEREST_PIN_RE.search(parts.path)
        if match and "pinterest." in parts.netloc.lower():
            return f"https://www.pinterest.com/pin/{match.group(1)}/"

        host = (parts.netloc or "").lower()
        short = (
            host == "pin.it"
            or host.endswith(".pin.it")
            or host == "api.pinterest.com"
            or "/url_shortener/" in parts.path
        )
        if not short:
            break
        try:
            current = _pinterest_location(current)
        except urllib.error.HTTPError as exc:
            raise DownloadError(
                f"Pinterest: короткая ссылка не раскрылась (HTTP {exc.code}). "
                "Пришлите полную ссылку вида https://www.pinterest.com/pin/<id>/"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DownloadError(
                "Pinterest: не удалось открыть короткую ссылку pin.it. "
                "Пришлите полную ссылку вида https://www.pinterest.com/pin/<id>/"
            ) from exc

    parts = urlsplit(current)
    match = _PINTEREST_PIN_RE.search(parts.path)
    if match:
        return f"https://www.pinterest.com/pin/{match.group(1)}/"

    raise DownloadError(
        "Pinterest: короткая ссылка pin.it уже не ведёт на пин (Pinterest "
        "отправил на главную). Такие ссылки истекают — пришлите полную ссылку "
        "из адресной строки вида https://www.pinterest.com/pin/<id>/"
    )


def _pinterest_opener() -> urllib.request.OpenerDirector:
    """Браузерный opener с cookies из COOKIES_FILE (для закрытых пинов)."""
    handlers: list[urllib.request.BaseHandler] = []
    if COOKIES_FILE:
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
            handlers.append(urllib.request.HTTPCookieProcessor(jar))
        except Exception as exc:  # noqa: BLE001 — без cookies тоже работает
            log.debug("Pinterest: cookies не загрузились: %s", exc)
    return urllib.request.build_opener(*handlers)


def _pinterest_page(url: str) -> str:
    opener = _pinterest_opener()
    request = urllib.request.Request(url, headers=_PINTEREST_HEADERS)
    try:
        with opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DownloadError("Pinterest: пин не найден или удалён.") from exc
        if exc.code in {401, 403}:
            raise DownloadError(
                "Pinterest: страница закрыта. Нужны cookies аккаунта "
                "(COOKIES_FILE) или ссылка на публичный пин."
            ) from exc
        raise DownloadError(f"Pinterest: страница ответила HTTP {exc.code}.") from exc
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(
            f"Pinterest: не удалось открыть страницу ({str(exc)[:120]})."
        ) from exc


def _pinterest_text(body: str) -> str:
    """Раскодирует JS/JSON-экранирование, чтобы ссылки стали обычными."""
    text = html.unescape(body).replace("\\/", "/")
    text = re.sub(r"\\u002[fF]", "/", text)
    text = re.sub(r"\\u0026", "&", text)
    return text


def _pinterest_original(image_url: str) -> str:
    """736x/… → originals/…: та же картинка, но в полном размере."""
    match = re.search(
        r"i\.pinimg\.com/(?:736x|564x|474x|236x)/"
        r"([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]+)\.(\w+)",
        image_url,
        re.I,
    )
    if match:
        a, b, c, name, ext = match.groups()
        return f"https://i.pinimg.com/originals/{a}/{b}/{c}/{name}.{ext}"
    return image_url


def _pinterest_main_window(text: str) -> str:
    """Кусок Relay-ответа с данными самого пина (без чужих рекомендаций)."""
    start = text.find('"v3GetPinQueryv2"')
    if start == -1:
        return text
    return text[start : start + 40_000]


def _pinterest_pin_data(body: str) -> dict | None:
    """Данные самого пина из встроенного JSON Pinterest.

    Страница кладёт ответ ``PinResource`` в
    ``<script data-test-id="resource-response-data">``. Это точный тип пина и
    его медиа — без соседних/похожих пинов, которые раньше подмешивались.
    """
    for match in re.finditer(
        r'<script[^>]*data-test-id="resource-response-data"[^>]*>(.*?)</script>',
        body,
        re.S | re.I,
    ):
        try:
            data = json.loads(match.group(1))
        except Exception:  # noqa: BLE001 — битый/обрезанный JSON
            continue
        if not isinstance(data, dict):
            continue
        resource = data.get("resource") or {}
        if resource.get("name") not in {"PinResource", "Pin", "PinResourceResponse"}:
            continue
        pin = (data.get("resource_response") or {}).get("data")
        if isinstance(pin, dict) and (pin.get("id") or pin.get("entityId")):
            return pin
    return None


def _pinterest_video_entries(pin: dict) -> list[tuple[str, int]]:
    """Все видео пина: (url, длительность в мс). MP4 раньше HLS."""
    entries: list[tuple[str, int]] = []

    def collect(video_list) -> None:
        if not isinstance(video_list, dict):
            return
        for variant in video_list.values():
            if not isinstance(variant, dict):
                continue
            url = variant.get("url") or ""
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            duration = variant.get("duration") or 0
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = 0
            entries.append((url, duration))

    collect((pin.get("videos") or {}).get("video_list"))
    for page in ((pin.get("story_pin_data") or {}).get("pages") or []):
        if not isinstance(page, dict):
            continue
        collect((page.get("video") or {}).get("video_list"))
        for block in page.get("blocks") or []:
            if isinstance(block, dict):
                collect((block.get("video") or {}).get("video_list"))
        for block in page.get("media") or []:
            if isinstance(block, dict):
                collect((block.get("video") or {}).get("video_list"))

    # MP4 — первыми, HLS — запасной вариант.
    entries.sort(key=lambda item: (".mp4" not in item[0].lower(), item[0]))
    return entries


def _pinterest_video_signature(pin: dict) -> str:
    for page in ((pin.get("story_pin_data") or {}).get("pages") or []):
        if isinstance(page, dict) and page.get("video_signature"):
            return str(page["video_signature"])
    videos = pin.get("videos") or {}
    signature = videos.get("video_signature")
    return str(signature) if signature else ""


def _pinterest_pin_title(pin: dict) -> str:
    for key in ("description", "grid_title", "title", "closeup_unified_title"):
        value = pin.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())
            if value:
                return value[:200]
    return ""


def _pinterest_pin_image(pin: dict) -> str:
    """Оригинал картинки пина (у карусели — первая картинка)."""
    # У story-пинов настоящая картинка лежит в странице, а не в обложке.
    for page in ((pin.get("story_pin_data") or {}).get("pages") or []):
        if not isinstance(page, dict):
            continue
        images = ((page.get("image") or {}).get("images")) or {}
        item = images.get("originals") or images.get("orig")
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])

    images = pin.get("images") or {}
    for key in ("orig", "originals", "1200x", "736x", "564x"):
        item = images.get(key)
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
        if isinstance(item, str) and item.startswith("http"):
            return item

    slots = ((pin.get("carousel_data") or {}).get("carousel_slots")) or []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_images = slot.get("images") or {}
        for key in ("orig", "originals", "1200x", "736x", "564x"):
            item = slot_images.get(key)
            if isinstance(item, dict) and item.get("url"):
                return _pinterest_original(str(item["url"]))
        video = _pinterest_video_entries(slot)
        if video:
            return ""

    signature = pin.get("image_signature")
    if signature:
        return _pinterest_signature_url(str(signature), "originals", "jpg")
    return ""


def _pinterest_preload(body: str) -> str:
    for tag in re.findall(r"<link\b[^>]*>", body, re.I):
        if "pin-image-preload" not in tag.lower():
            continue
        href = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if href:
            value = html.unescape(href.group(1)).strip()
            return "https:" + value if value.startswith("//") else value
    return ""


def _pinterest_signature_url(signature: str, size: str, ext: str) -> str:
    return (
        f"https://i.pinimg.com/{size}/{signature[:2]}/{signature[2:4]}/"
        f"{signature[4:6]}/{signature}.{ext}"
    )


def _pinterest_mp4_url(signature: str, variant: str = "720w") -> str:
    """MP4-вариант видео по подписи: у Pinterest это тот же хеш, что и у картинок."""
    return (
        f"https://v1.pinimg.com/videos/iht/expMp4/{signature[:2]}/{signature[2:4]}/"
        f"{signature[4:6]}/{signature}_{variant}.mp4"
    )


def _pinterest_media_candidates(body: str) -> list[tuple[str, str]]:
    """Упорядоченные варианты медиа: сначала основное, потом запасные.

    Pinterest отдаёт картинку в нескольких размерах, а видео — несколькими
    mp4-вариантами и HLS. Если один URL отвечает 403, пробуем следующий.
    """
    text = _pinterest_text(body)
    window = _pinterest_main_window(text)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, url: str) -> None:
        value = html.unescape(url or "").strip()
        if value.startswith("//"):
            value = "https:" + value
        if not value.startswith("http") or value in seen:
            return
        seen.add(value)
        candidates.append((kind, value))

    # 1) Встроенный JSON пина — самый точный источник: в нём только этот пин,
    #    поэтому картинка не превращается в видео из соседней рекомендации.
    pin = _pinterest_pin_data(body)
    if pin is not None:
        entries = _pinterest_video_entries(pin)
        if entries:
            for url, _ in entries:
                if ".mp4" in url.lower():
                    add("video", url)
            signature = _pinterest_video_signature(pin)
            if signature:
                add("video", _pinterest_mp4_url(signature))
            for url, _ in entries:
                if ".mp4" not in url.lower():
                    add("hls", url)
            if candidates:
                return candidates

        image = _pinterest_pin_image(pin)
        if image:
            add("image", image)
        preload = _pinterest_preload(body)
        if preload:
            add("image", preload)
            add("image", _pinterest_original(preload))
        if candidates:
            return candidates

    # 2) Страница без JSON: старый разбор по разметке.
    videos = []
    hevc = []
    for candidate in _PINTEREST_VIDEO_RE.findall(window):
        low = candidate.lower()
        if "hevc" in low or "h265" in low:
            hevc.append(candidate)
        else:
            videos.append(candidate)

    if videos or hevc:
        def rank(item: str) -> int:
            low = item.lower()
            score = 0
            if "expmp4" in low:
                score -= 60
            if "720w" in low or "720p" in low:
                score -= 30
            elif "540w" in low or "540p" in low:
                score -= 20
            elif "480w" in low:
                score -= 15
            if "_t1" in low or "_t2" in low:
                score += 30
            return score

        for item in sorted(set(videos), key=rank):
            add("video", item)
        for item in sorted(set(hevc), key=rank):
            add("video", item)
        for item in _PINTEREST_HLS_RE.findall(window):
            add("hls", item)
        return candidates

    # Основная картинка: imageSignature даёт точный хеш самого пина.
    preload = _pinterest_preload(body)
    ext_match = re.search(r"\.(jpe?g|png|webp|gif)(?:\?|$)", preload, re.I)
    ext = ext_match.group(1).lower() if ext_match else "jpg"
    if ext == "jpeg":
        ext = "jpg"
    sig_match = re.search(
        r'"imageSignature"\s*:\s*"([0-9a-f]{16,})"', window, re.I
    )
    signature = sig_match.group(1) if sig_match else ""
    if signature:
        add("image", _pinterest_signature_url(signature, "originals", ext))
    if preload:
        add("image", preload)
        add("image", _pinterest_original(preload))

    for key in ("og:image:secure_url", "og:image", "twitter:image"):
        value = _meta_value(body, key)
        if value:
            add("image", _pinterest_original(value))

    for item in _PINTEREST_ORIGINAL_RE.findall(window)[:3]:
        add("image", item)
    for item in _PINTEREST_IMAGE_RE.findall(window)[:5]:
        add("image", _pinterest_original(item))

    if not candidates:
        # Последний шанс: страница без imageSignature/preload.
        for item in _PINTEREST_ORIGINAL_RE.findall(text)[:5]:
            add("image", item)
        for item in _PINTEREST_IMAGE_RE.findall(text)[:5]:
            add("image", _pinterest_original(item))
    return candidates


def _pinterest_media(body: str) -> tuple[str, str] | None:
    """Первый вариант медиа — для тестов и диагностики."""
    candidates = _pinterest_media_candidates(body)
    return candidates[0] if candidates else None


def _js_unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:  # noqa: BLE001
        return value


def _pinterest_title(body: str, pin_id: str) -> str:
    def clean(value: str) -> str:
        return " ".join(value.split())[:200]

    pin = _pinterest_pin_data(body)
    if pin is not None:
        value = _pinterest_pin_title(pin)
        if value:
            return clean(value)

    for key in ("og:title", "twitter:title"):
        value = _meta_value(body, key)
        low = value.lower()
        if value and low not in {"pinterest", "пин на доске"} and not low.startswith(
            "пин на доске"
        ):
            return clean(value)

    text = _pinterest_text(body)
    start = text.find('"v3GetPinQueryv2"')
    window = text[start : start + 60_000] if start != -1 else text[:60_000]
    for pattern in (
        r'"description"\s*:\s*"((?:[^"\\]|\\.){3,400})"',
        r'"grid_title"\s*:\s*"((?:[^"\\]|\\.){1,160})"',
        r'"title"\s*:\s*"((?:[^"\\]|\\.){1,160})"',
    ):
        match = re.search(pattern, window)
        if not match:
            continue
        value = _js_unescape(match.group(1)).strip()
        if value and value.lower() not in {"pinterest", "pin"}:
            return clean(value)

    match = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
    if match:
        value = html.unescape(match.group(1)).strip()
        if value and value.lower() not in {"pinterest", "пин на доске"}:
            return clean(value)
    return f"Pinterest {pin_id}"


def _pinterest_download_direct(
    media_url: str,
    dest: Path,
    referer: str,
    on_progress=None,
    cancel=None,
) -> int:
    request = urllib.request.Request(
        media_url,
        headers={
            **_PINTEREST_HEADERS,
            "Referer": referer,
            "Origin": "https://www.pinterest.com",
            "Accept": "*/*",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        },
    )
    opener = _pinterest_opener()
    with opener.open(request, timeout=120) as response:
        total = response.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        try:
            with dest.open("wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    return done


def _pinterest_curl_download(
    media_url: str,
    dest: Path,
    referer: str,
    proxy: str,
    on_progress=None,
    cancel=None,
) -> int:
    """Запасной путь через WARP: CDN Pinterest иногда рвёт прямой коннект."""
    curl = shutil.which("curl")
    if not curl:
        raise DownloadError("Pinterest: CDN не отдал файл, а curl для обхода нет.")
    cmd = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "--max-time",
        "300",
        "--proxy",
        proxy,
        "--user-agent",
        _PINTEREST_HEADERS["User-Agent"],
        "--referer",
        referer,
        "--header",
        "Origin: https://www.pinterest.com",
        "--output",
        str(dest),
        media_url,
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd[1:1] = ["--cookie", str(COOKIES_FILE)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise DownloadError(f"Pinterest: curl не запустился ({exc}).") from exc

    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise DownloadCancelled()
            if on_progress and dest.exists():
                on_progress(dest.stat().st_size, None)
            time.sleep(0.25)
        stderr = (proc.stderr.read() or b"").decode("utf-8", "replace") if proc.stderr else ""
    finally:
        if proc.poll() is None:
            proc.kill()

    if proc.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        raise DownloadError(
            f"Pinterest: CDN не отдал файл даже через WARP ({stderr[:160]})."
        )
    return dest.stat().st_size


def _pinterest_download_file(
    media_url: str,
    dest: Path,
    referer: str,
    on_progress=None,
    cancel=None,
) -> int:
    try:
        return _pinterest_download_direct(
            media_url, dest, referer, on_progress, cancel
        )
    except DownloadCancelled:
        raise
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        proxy = _yt_proxy()
        # 403 бывает и «не тому региону/адресу»: пробуем через WARP, если он есть.
        if proxy and exc.code in {403, 429, 451}:
            log.warning("Pinterest: HTTP %s, пробую WARP", exc.code)
            try:
                return _pinterest_curl_download(
                    media_url, dest, referer, proxy, on_progress, cancel
                )
            except DownloadCancelled:
                raise
            except DownloadError as warp_exc:
                log.warning("Pinterest: через WARP тоже не вышло (%s)", warp_exc)
        raise DownloadError(
            f"Pinterest: файл не скачался (HTTP {exc.code})."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — обрыв на CDN лечим WARP
        dest.unlink(missing_ok=True)
        proxy = _yt_proxy()
        if not proxy:
            raise DownloadError(
                f"Pinterest: CDN не отдал файл ({str(exc)[:120]})."
            ) from exc
        log.warning("Pinterest: прямой CDN не отдал файл (%s), пробую WARP", exc)
        return _pinterest_curl_download(
            media_url, dest, referer, proxy, on_progress, cancel
        )


def _pinterest_hls_download(
    media_url: str,
    out_dir: Path,
    referer: str,
    on_progress=None,
    cancel=None,
) -> tuple[Path, int]:
    """HLS-вариант: yt-dlp сам соберёт сегменты в mp4 (нужен ffmpeg)."""
    opts = _opts() | {
        "noplaylist": True,
        "outtmpl": str(out_dir / "pinterest-hls.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "progress_hooks": _progress_hook(on_progress, None, cancel),
        "http_headers": {
            **_PINTEREST_HEADERS,
            "Referer": referer,
            "Origin": "https://www.pinterest.com",
        },
        "overwrites": True,
    }
    try:
        _extract_with_rescue(media_url, opts, download=True, cancel=cancel)
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        for leftover in out_dir.glob("pinterest-hls.*"):
            leftover.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled() from exc
        raise DownloadError(
            f"Pinterest: HLS не скачался ({str(exc)[:160]})."
        ) from exc

    produced = [
        p
        for p in out_dir.glob("pinterest-hls.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not produced:
        raise DownloadError("Pinterest: HLS не дал файл.")
    src = max(produced, key=lambda p: p.stat().st_size)
    dest = out_dir / "pinterest.mp4"
    if src != dest:
        dest.unlink(missing_ok=True)
        src.replace(dest)
    return dest, dest.stat().st_size


def _extract_mp3(src: Path, dest: Path) -> bool:
    """Вытащить звук из видео Pinterest через ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size < 10_000:
        log.warning("Pinterest: ffmpeg не извлёк звук: %s", result.stderr[-300:])
        return False
    return True


def _pinterest_download_sync(
    url: str,
    out_dir: Path,
    mode: str = "auto",
    on_progress=None,
    cancel=None,
) -> tuple[Path, dict]:
    """Пин без входа: короткая ссылка → страница → видео или картинка."""
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    out_dir.mkdir(parents=True, exist_ok=True)

    canonical = _pinterest_canonical(url)
    pin_match = _PINTEREST_PIN_RE.search(urlsplit(canonical).path)
    pin_id = pin_match.group(1) if pin_match else ""

    body = _pinterest_page(canonical)
    candidates = _pinterest_media_candidates(body)
    if not candidates:
        raise DownloadError(
            "Pinterest: на странице пина не нашлось ни видео, ни картинки. "
            "Возможно, это текстовый пин или страница не догрузилась."
        )

    primary_kind = candidates[0][0]
    if primary_kind == "image" and mode == "audio":
        raise DownloadError(
            "Pinterest: в этом пине картинка, звуковой дорожки нет. "
            "Выберите «Фото-видео» — пришлю изображение."
        )

    info = {
        "id": pin_id,
        "title": _pinterest_title(body, pin_id),
        "uploader": "",
        "duration": 0,
        "webpage_url": canonical,
    }
    pin = _pinterest_pin_data(body)
    if pin is not None:
        info["duration"] = max(
            (ms // 1000 for _, ms in _pinterest_video_entries(pin)), default=0
        )

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    last_error: DownloadError | None = None

    for kind, media_url in candidates:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()

        try:
            if kind == "hls":
                path, size = _pinterest_hls_download(
                    media_url, out_dir, canonical, on_progress, cancel
                )
            else:
                if kind == "image":
                    ext = _guess_ext(media_url)
                    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                        ext = ".jpg"
                    if ext == ".jpeg":
                        ext = ".jpg"
                    path = out_dir / f"pinterest{ext}"
                else:
                    path = out_dir / "pinterest.mp4"
                size = _pinterest_download_file(
                    media_url, path, canonical, on_progress, cancel
                )
                if kind == "image" and size < 1024:
                    path.unlink(missing_ok=True)
                    raise DownloadError("Pinterest: картинка пришла пустой.")
                if kind == "video" and size < 10_000:
                    path.unlink(missing_ok=True)
                    raise DownloadError("Pinterest: видео пришло пустым.")
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — пробуем следующий вариант
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled() from exc
            last_error = (
                exc
                if isinstance(exc, DownloadError)
                else DownloadError(
                    f"Pinterest: файл не скачался ({str(exc)[:120]})."
                )
            )
            log.warning(
                "Pinterest: вариант %s не подошёл (%s)", media_url, last_error
            )
            continue

        if kind == "image":
            log.info("Pinterest: картинка %s (%s байт)", media_url, size)
            return path, info

        if mode == "audio":
            audio = out_dir / "pinterest.mp3"
            if not _extract_mp3(path, audio):
                path.unlink(missing_ok=True)
                audio.unlink(missing_ok=True)
                raise DownloadError(
                    "Pinterest: не удалось извлечь звук (нужен ffmpeg). "
                    "Выберите «Фото-видео» — пришлю видео."
                )
            path.unlink(missing_ok=True)
            log.info("Pinterest: звук из видео %s", media_url)
            return audio, info

        if size > max_bytes:
            # Этот вариант слишком большой — пробуем качество пониже.
            path.unlink(missing_ok=True)
            last_error = DownloadError(
                f"Pinterest: видео больше {MAX_UPLOAD_MB} МБ — Telegram не "
                "примет. Выберите «Только звук»."
            )
            log.warning("Pinterest: %s, пробую вариант поменьше", last_error)
            continue

        log.info("Pinterest: видео %s (%s байт)", media_url, size)
        return path, info

    raise last_error or DownloadError(
        "Pinterest: не удалось скачать файл со страницы пина."
    )


def _dns_retry(func, attempts: int = 3, delay: float = 1.5):
    """DNS через VPN иногда отвечает «getaddrinfo failed» — повторяем запрос."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            text = f"{exc} {getattr(exc, 'cause', '')}".lower()
            if "getaddrinfo failed" not in text and "errno 11001" not in text:
                raise
            last = exc
            log.warning("DNS не ответил (попытка %s/%s), повторяю", attempt + 1, attempts)
            time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def service_name(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    for domain, name in _SERVICES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host if host else ""


def friendly_error(exc: Exception, url: str | None = None) -> str:
    """Короткая понятная ошибка: «Сервис: что случилось» + максимум одна подсказка."""
    low = str(exc).lower()
    prefix = f"{service_name(url)}: " if url else ""

    if "requested format is not available" in low:
        if url and is_youtube(url):
            return (
                f"{prefix}не отдал форматы для скачивания.\n"
                "Выполните /yt_debug — он покажет рабочий player_client."
            )
        return f"{prefix}нет подходящего качества.\nПопробуйте другую ссылку."
    if "no video could be found" in low:
        return f"{prefix}в этом посте нет видео, а картинку получить не удалось."
    if "sign in to confirm" in low or "not a bot" in low:
        if url and is_youtube(url):
            return YOUTUBE_BOT_CHECK
        return (
            f"{prefix}не пускает сервер без входа.\n"
            "Для Instagram/TikTok помогают cookies аккаунта:\n"
            "COOKIES_FILE или YT_COOKIES."
        )
    if "failed to extract any player response" in low:
        if url and is_youtube(url):
            return (
                f"{prefix}не отдал видео: сервер не получил player response.\n"
                "Проверьте cookies (COOKIES_FILE) и выполните /yt_debug — "
                "он покажет, какой player_client работает."
            )
    if "too many requests" in low or "http error 429" in low or "rate limit" in low:
        return f"{prefix}ограничил запросы.\nПодождите пару минут."
    if "logged-in" in low or "login" in low or "cookies" in low or "authentication" in low:
        return f"{prefix}требует вход в аккаунт.\nБез cookies приватное недоступно."
    if "private" in low or "deleted" in low or "removed" in low:
        return f"{prefix}видео приватное или удалено."
    if "http error 404" in low or "not found" in low:
        return f"{prefix}по ссылке ничего не найдено."
    if "http error 403" in low or "forbidden" in low:
        if url and _is_hitmo(url):
            return "Hitmo не отдал файл.\nПопробуйте другую ссылку или трек."
        return f"{prefix}запретил доступ (403).\nСкорее всего, нужны cookies."
    if "unsupported url" in low or "no suitable extractor" in low:
        if url and _is_vk(url):
            return (
                "VK: не понял ссылку.\n"
                "Пришлите ссылку на видео (vk.com/video…), клип или пост, где есть видео."
            )
        return f"{prefix}этот сервис не поддерживается."
    if "geo" in low and "block" in low:
        return f"{prefix}заблокировано в регионе сервера.\nПопробуйте прокси."
    if "timed out" in low or "timeout" in low:
        return f"{prefix}не ответил вовремя.\nПопробуйте ещё раз."
    if "unavailable" in low or "not available" in low:
        return f"{prefix}видео недоступно (удалено или скрыто)."
    if "no video formats" in low or "unable to extract" in low:
        return f"{prefix}не удалось найти видео на странице."
    if "internal server error" in low or "http error 5" in low:
        return f"{prefix}сломался на своей стороне (5xx).\nПопробуйте позже."
    return f"{prefix}не удалось скачать.\nСсылка могла устареть или сервис недоступен."


# ---------------------------------------------------------------- резолв стримингов


def is_music_link(url: str) -> bool:
    """Ссылка на Spotify/Apple Music/Яндекс.Музыку, которую надо искать по названию."""
    return bool(
        SPOTIFY_RE.search(url)
        or SPOTIFY_ALBUM_RE.search(url)
        or APPLE_MUSIC_RE.search(url)
        or YANDEX_MUSIC_RE.search(url)
        or YANDEX_ALBUM_RE.search(url)
    )


def _fetch_text(
    url: str,
    user_agent: str,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> str:
    merged = {
        "User-Agent": user_agent,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if headers:
        merged.update(headers)
    request = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _spotify_query_sync(url: str) -> str | None:
    """Spotify отдаёт og:title/og:description только «ботовым» User-Agent.

    Работает для ссылок на трек: страницы альбомов и плейлистов Spotify
    ботам отдаёт как заглушку, их разобрать нельзя.
    """
    match = SPOTIFY_RE.search(url)
    if not match:
        return None

    body = _fetch_text(f"https://open.spotify.com/track/{match.group(1)}", "Twitterbot/1.0")
    meta = {key: html.unescape(value) for key, value in _OG_RE.findall(body)}
    title = (meta.get("title") or "").strip()
    description = (meta.get("description") or "").strip()
    if not title or "spotify is all the music" in (title + description).lower():
        return None
    artist = description.split("·")[0].strip()
    return f"{artist} {title}".strip() or None


def _apple_query_sync(url: str) -> str | None:
    """iTunes Search API — бесплатно и без ключей."""
    match = APPLE_MUSIC_RE.search(url)
    if not match:
        return None
    country = (match.group(1) or "us").lower()
    kind, base_id, track_id = match.group(2), match.group(3), match.group(4)
    lookup_id = track_id or base_id
    api = (
        f"https://itunes.apple.com/lookup?id={lookup_id}"
        f"&entity=song&country={country}"
    )
    data = json.loads(_fetch_text(api, "Mozilla/5.0"))
    results = data.get("results") or []

    for item in results:
        if item.get("wrapperType") == "track" and item.get("trackName"):
            return f"{item.get('artistName', '')} {item['trackName']}".strip()

    # ссылка на альбом без конкретного трека — ищем по исполнителю и альбому
    if kind == "album" and not track_id:
        for item in results:
            if item.get("wrapperType") == "collection" and item.get("collectionName"):
                return f"{item.get('artistName', '')} {item['collectionName']}".strip()
    return None


_YANDEX_API_HOSTS = (
    "api.music.yandex.net",
    "api.music.yandex.com",
    "api.music.yandex.ru",
)
_YANDEX_PAGE_HOSTS = ("music.yandex.ru", "music.yandex.com")
# VK и Яндекс отдают готовые OG-теги «ботам предпросмотра» даже с чужих IP
_YANDEX_PAGE_UAS = (
    HITMO_HEADERS["User-Agent"],
    "TelegramBot (like TwitterBot)",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
)
_YANDEX_API_HEADERS = {
    **HITMO_HEADERS,
    "Accept": "application/json",
    "X-Yandex-Music-Client": "YandexMusicWeb/1.0",
}
# бюджет на весь разбор: с датацентрового IP часть запросов может висеть
_YANDEX_BUDGET = 22.0


def _yandex_paths(track_id: str | None, album_id: str | None) -> list[str]:
    paths: list[str] = []
    if track_id:
        paths.append(f"/track/{track_id}")
        if album_id:
            paths.append(f"/album/{album_id}/track/{track_id}")
    if album_id:
        paths.append(f"/album/{album_id}")
    return paths


def _yandex_meta(body: str, prop: str) -> str:
    """Достаём og-тег, не завязываясь на порядок атрибутов."""
    return _meta_value(body, f"og:{prop}")


def _yandex_og_query(body: str) -> str | None:
    """og:title — название, og:description — «Исполнитель • Трек • 2024»."""
    title = _yandex_meta(body, "title")
    description = _yandex_meta(body, "description")
    title = re.sub(r"\s*слушать онлайн.*$", "", title, flags=re.I).strip()
    if not title or "яндекс музыка" in title.lower():
        return None  # заглушка/капча вместо страницы трека
    artist = description.split("•")[0].strip()
    if "яндекс музыка" in artist.lower() or "слушать" in artist.lower():
        artist = ""
    if artist and title.lower().startswith(artist.lower()):
        return title
    return f"{artist} {title}".strip() or None


def _yandex_item_query(result) -> str | None:
    if isinstance(result, list):
        item = result[0] if result else None
    else:
        item = result
    if not isinstance(item, dict):
        return None
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or []).strip(", ")
    title = item.get("title") or ""
    return f"{artists} {title}".strip() or None


def _yandex_api_query(
    track_id: str | None, album_id: str | None, deadline: float
) -> str | None:
    path = f"/tracks/{track_id}" if track_id else f"/albums/{album_id}"
    last: Exception | None = None
    answered = False
    for host in _YANDEX_API_HOSTS:
        left = deadline - time.monotonic()
        if left <= 1:
            break
        try:
            body = _fetch_text(
                f"https://{host}{path}",
                HITMO_HEADERS["User-Agent"],
                timeout=min(10.0, left),
                headers=_YANDEX_API_HEADERS,
            )
            query = _yandex_item_query(json.loads(body).get("result"))
            answered = True
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.debug("Яндекс.Музыка: API %s не ответил: %s", host, exc)
            continue
        if query:
            return query
        log.debug("Яндекс.Музыка: API %s вернул пустой результат", host)
    if not answered and last is not None:
        raise last
    return None


def _yandex_page_query(
    track_id: str | None, album_id: str | None, deadline: float
) -> str | None:
    """Запасной путь: og-теги страницы трека (API с Render иногда отдаёт 403)."""
    paths = _yandex_paths(track_id, album_id)

    last: Exception | None = None
    answered = False
    for ua in _YANDEX_PAGE_UAS:
        for host in _YANDEX_PAGE_HOSTS:
            for path in paths:
                left = deadline - time.monotonic()
                if left <= 1:
                    break
                try:
                    body = _fetch_text(
                        f"https://{host}{path}",
                        ua,
                        timeout=min(8.0, left),
                    )
                    answered = True
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    log.debug(
                        "Яндекс.Музыка: страница %s%s не открылась: %s", host, path, exc
                    )
                    continue
                query = _yandex_og_query(body)
                if query:
                    log.info("Яндекс.Музыка: метаданные взяты со страницы %s%s", host, path)
                    return query
        if deadline - time.monotonic() <= 1:
            break
    if not answered and last is not None:
        raise last
    return None


def _yandex_proxy_query(
    track_id: str | None, album_id: str | None, deadline: float
) -> str | None:
    """Последний шанс: страница через прокси Google-переводчика.

    music.yandex.ru → music-yandex-ru.translate.goog: запрос выполняют серверы
    Google, которых Яндекс не блокирует, а OG-теги остаются в HTML как есть.
    """
    paths = _yandex_paths(track_id, album_id)
    last: Exception | None = None
    answered = False
    for host in _YANDEX_PAGE_HOSTS:
        proxy = "https://" + host.replace(".", "-") + ".translate.goog"
        for path in paths:
            left = deadline - time.monotonic()
            if left <= 1:
                break
            url = f"{proxy}{path}?_x_tr_sl=ru&_x_tr_tl=en&_x_tr_hl=en"
            try:
                body = _fetch_text(
                    url,
                    HITMO_HEADERS["User-Agent"],
                    timeout=min(10.0, left),
                )
                answered = True
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.debug("Яндекс.Музыка: прокси %s не ответил: %s", proxy, exc)
                continue
            query = _yandex_og_query(body)
            if query:
                log.info("Яндекс.Музыка: метаданные получены через прокси %s", proxy)
                return query
    if not answered and last is not None:
        raise last
    return None


def _yandex_query_sync(url: str, problems: list[str] | None = None) -> str | None:
    """Метаданные Яндекс.Музыки: API → OG-теги страницы → прокси.

    Публичный API работает без токена, но с датацентровых IP (Render) бывает
    403/таймаут, поэтому пробуем несколько хостов, «ботовые» User-Agent и
    страницу через прокси.
    """
    match = YANDEX_MUSIC_RE.search(url)
    album_match = YANDEX_ALBUM_RE.search(url)
    track_id = match.group(1) if match else None
    album_id = album_match.group(1) if album_match else None
    if not track_id and not album_id:
        return None

    reasons = problems if problems is not None else []
    deadline = time.monotonic() + _YANDEX_BUDGET
    for name, func in (
        ("API", _yandex_api_query),
        ("страница", _yandex_page_query),
        ("прокси", _yandex_proxy_query),
    ):
        try:
            query = func(track_id, album_id, deadline)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if query:
            return query
        reasons.append(f"{name}: пустой ответ")

    log.warning(
        "Яндекс.Музыка: не удалось разобрать %s (%s)",
        url,
        "; ".join(reasons) or "нет данных",
    )
    return None


async def resolve_music_link_detailed(url: str) -> tuple[str | None, str]:
    """Как resolve_music_link, но ещё и причина отказа (для сообщения)."""
    reasons: list[str] = []
    if SPOTIFY_RE.search(url) or SPOTIFY_ALBUM_RE.search(url):
        func = _spotify_query_sync
    elif APPLE_MUSIC_RE.search(url):
        func = _apple_query_sync
    elif YANDEX_MUSIC_RE.search(url) or YANDEX_ALBUM_RE.search(url):
        func = lambda link: _yandex_query_sync(link, reasons)  # noqa: E731
    else:
        return None, ""

    try:
        query = await asyncio.wait_for(asyncio.to_thread(func, url), timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning("music link resolve failed: %s: %s: %s", url, type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    return query, "; ".join(reasons)


async def resolve_music_link(url: str) -> str | None:
    """Превращает ссылку стриминга в поисковый запрос «исполнитель название»."""
    query, _ = await resolve_music_link_detailed(url)
    return query


# ---------------------------------------------------------------- поиск музыки


def _search_sync(query: str, provider: str, limit: int) -> list[Track]:
    prefix = "ytsearch" if provider == "yt" else "scsearch"
    opts = _opts() | {"extract_flat": "in_playlist", "skip_download": True}

    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)

    tracks: list[Track] = []
    for entry in (data or {}).get("entries") or []:
        if not entry:
            continue

        title = (entry.get("title") or "").strip()
        if not title:
            continue

        if entry.get("live_status") == "is_live":
            continue

        duration = int(entry.get("duration") or 0)
        # отсекаем обрезки и часовые подкасты
        if duration and not (20 <= duration <= 20 * 60):
            continue

        url = (entry.get("webpage_url") or entry.get("url") or "").strip()
        video_id = str(entry.get("id") or url)

        if url and not url.startswith("http"):
            # плоский поиск YouTube отдаёт id — собираем короткую ссылку
            url = f"https://youtu.be/{video_id}"
        if not url.startswith("http"):
            continue

        tracks.append(
            Track(
                provider=provider,
                video_id=video_id,
                url=url,
                title=title,
                uploader=(entry.get("uploader") or entry.get("channel") or "").strip(),
                duration=duration,
            )
        )
    return tracks


async def _search_one(query: str, provider: str, limit: int) -> list[Track]:
    try:
        if provider == "hm":
            return await asyncio.to_thread(_hitmo_search_sync, query, limit)
        if provider == "zv":
            return await asyncio.wait_for(
                asyncio.to_thread(_zvyki_search_sync, query, limit), timeout=15.0
            )
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, provider, limit), timeout=25.0
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("%s search failed for %r: %s", provider, query, exc)
        return []


# ---------------------------------------------------------------- Hitmo


def _hitmo_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def _is_hitmo(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    return host.endswith("hitmoz.org") or host.endswith("deliciousoranges.com")


def _hitmo_get(
    url: str,
    opener,
    referer: str = HITMO_BASE + "/",
    attempts: int = 3,
    timeout: float = 30.0,
    on_progress=None,
    cancel=None,
) -> bytes:
    """Запрос к Hitmo с браузерными заголовками и повторами.

    WAF отвечает 403, если заголовки не похожи на браузерные, и любит
    обрывать SSL — поэтому пробуем несколько раз, обновляя cookie-сессию.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        request = urllib.request.Request(url, headers={**HITMO_HEADERS, "Referer": referer})
        try:
            with opener.open(request, timeout=timeout) as response:
                if on_progress is None and cancel is None:
                    return response.read()
                total = response.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else None
                chunks: list[bytes] = []
                done = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (401, 403, 429, 500, 502, 503):
                raise
            log.warning("Hitmo: HTTP %s (попытка %s/%s)", exc.code, attempt + 1, attempts)
        except Exception as exc:  # noqa: BLE001 — SSL/таймауты лечим повтором
            last = exc
            log.warning("Hitmo: %s (попытка %s/%s)", exc, attempt + 1, attempts)

        time.sleep(0.8 * (attempt + 1))
        # заново открываем главную: обновляем cookie и «представляемся» браузером
        try:
            opener.open(
                urllib.request.Request(HITMO_BASE + "/", headers=HITMO_HEADERS),
                timeout=20,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    raise last  # type: ignore[misc]


def _hitmo_track_from_meta(meta: dict, duration: int = 0) -> Track | None:
    url = (meta.get("url") or "").strip()
    title = (meta.get("title") or "").strip()
    if url.startswith("/"):
        url = HITMO_BASE + url
    if not url.startswith("http") or not title:
        return None
    return Track(
        provider="hm",
        video_id=str(meta.get("id") or url),
        url=url,
        title=title,
        uploader=(meta.get("artist") or "").strip(),
        duration=duration,
    )


def _hitmo_search_sync(query: str, limit: int) -> list[Track]:
    """Поиск на ru.hitmoz.org: треки берём из JSON в атрибуте data-musmeta."""
    opener = _hitmo_opener()
    page = f"{HITMO_BASE}/search?{urllib.parse.urlencode({'q': query})}"
    body = _hitmo_get(page, opener, attempts=2, timeout=12).decode("utf-8", "replace")

    tracks: list[Track] = []
    for match in _HITMO_ITEM_RE.finditer(body):
        try:
            meta = json.loads(html.unescape(match.group(1)))
        except (ValueError, TypeError):
            continue

        tail = body[match.end() : match.end() + 3000]
        found = _HITMO_TIME_RE.search(tail)
        duration = int(found.group(1)) * 60 + int(found.group(2)) if found else 0

        track = _hitmo_track_from_meta(meta, duration)
        if not track:
            continue
        tracks.append(track)
        if len(tracks) >= limit:
            break

    return tracks


def _hitmo_song_track_sync(url: str) -> Track:
    """Страница трека (/song/<id>): достаём data-musmeta нужного трека."""
    opener = _hitmo_opener()
    body = _hitmo_get(url, opener, attempts=2, timeout=20).decode("utf-8", "replace")

    song_match = _HITMO_SONG_RE.search(url)
    song_id = song_match.group(1) if song_match else ""
    fallback: Track | None = None

    for match in _HITMO_ITEM_RE.finditer(body):
        try:
            meta = json.loads(html.unescape(match.group(1)))
        except (ValueError, TypeError):
            continue
        track = _hitmo_track_from_meta(meta)
        if not track:
            continue
        if song_id and str(meta.get("id", "")).endswith(song_id):
            return track
        if fallback is None:
            fallback = track

    if fallback is None:
        raise DownloadError("Hitmo: не удалось разобрать страницу трека.")
    return fallback


def _safe_track_name(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or "track"


def _hitmo_download_sync(
    track: Track, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    """Hitmo отдаёт готовый mp3, поэтому качаем напрямую, без yt-dlp."""
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    opener = _hitmo_opener()
    try:
        _hitmo_get(HITMO_BASE + "/", opener, attempts=2, cancel=cancel)  # cookie сессии
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — без cookie тоже может сработать
        log.debug("Hitmo: не удалось открыть главную: %s", exc)

    data = _hitmo_get(
        track.url,
        opener,
        referer=HITMO_BASE + "/",
        on_progress=on_progress,
        cancel=cancel,
    )
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    if len(data) < 50_000:
        raise DownloadError("Hitmo отдал пустой файл. Попробуйте другой трек.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise DownloadError(f"Файл больше {MAX_UPLOAD_MB} МБ — Telegram не примет.")

    name = _safe_track_name(f"{track.uploader} - {track.title}")
    path = out_dir / f"{name}.mp3"
    path.write_bytes(data)
    return path, {
        "title": track.title,
        "track": track.title,
        "artist": track.uploader,
        "uploader": track.uploader,
        "duration": track.duration,
    }


def _hitmo_from_url_sync(
    url: str, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    """Ссылка на Hitmo из чата: страница трека или прямой mp3."""
    if _HITMO_MP3_RE.search(urlsplit(url).path):
        stem = urllib.parse.unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        title = re.sub(r"\.mp3$", "", stem, flags=re.I)
        title = re.sub(r"_(?:b[0-9a-f]{6,}|\d{5,})$", "", title)
        title = title.replace("_", " ").strip() or "Hitmo"
        track = Track(provider="hm", video_id=url, url=url, title=title, uploader="", duration=0)
    else:
        track = _hitmo_song_track_sync(url)
    return _hitmo_download_sync(track, out_dir, on_progress, cancel)


# ---------------------------------------------------------------- Zvyki


def _zvyki_get(url: str, timeout: float = 30.0) -> str:
    """Страница zvyki.com обычным GET: сайт не требует cookies, только Referer."""
    # в ссылке из чата могут остаться кириллица и пробелы — urllib такое не умеет
    url = urllib.parse.quote(url, safe=":/?&=%(),._-~+#")
    last: Exception | None = None
    for _ in range(2):
        request = urllib.request.Request(
            url, headers={**ZVYKI_HEADERS, "Referer": ZVYKI_BASE + "/"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last if last else DownloadError("Zvyki недоступен.")


def _is_zvyki(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    return host.endswith("zvyki.com")


def _zvyki_parse_tracks(body: str, limit: int) -> list[Track]:
    """Треки лежат в <li class="track" data-id data-mp3 data-duration>."""
    tracks: list[Track] = []
    for block in _ZV_TRACK_RE.finditer(body):
        chunk = block.group(1)
        mp3 = _ZV_MP3_RE.search(chunk)
        track_id = _ZV_ID_RE.search(chunk)
        if not mp3 or not track_id:
            continue

        artist_match = _ZV_ARTIST_RE.search(chunk)
        title_match = _ZV_TITLE_RE.search(chunk)
        artist = html.unescape(artist_match.group(1)).strip() if artist_match else ""
        title = html.unescape(title_match.group(1)).strip() if title_match else ""
        if not title:
            continue

        duration = 0
        dur_match = _ZV_DUR_RE.search(chunk)
        if dur_match:
            duration = int(dur_match.group(1)) // 1000

        tracks.append(
            Track(
                provider="zv",
                video_id=track_id.group(1),
                url=html.unescape(mp3.group(1)),
                title=title,
                uploader=artist,
                duration=duration,
            )
        )
        if len(tracks) >= limit:
            break
    return tracks


def _zvyki_search_sync(query: str, limit: int) -> list[Track]:
    page = f"{ZVYKI_BASE}/search/q/{urllib.parse.quote_plus(query)}/"
    return _zvyki_parse_tracks(_zvyki_get(page, timeout=20), limit)


def _zvyki_song_track_sync(url: str) -> Track:
    """Страница трека: берём блок с нужным data-id."""
    body = _zvyki_get(url)
    song_match = _ZV_SONG_ID_RE.search(url)
    song_id = song_match.group(1) if song_match else ""

    if song_id:
        pattern = re.compile(
            r'<li class="track"[^>]*data-id="' + re.escape(song_id) + r'".*?</li>', re.S
        )
        found = pattern.search(body)
        if found:
            parsed = _zvyki_parse_tracks(found.group(0), 1)
            if parsed:
                return parsed[0]

    parsed = _zvyki_parse_tracks(body, 1)
    if parsed:
        return parsed[0]
    raise DownloadError("Zvyki: не удалось разобрать страницу трека.")


def _zvyki_fetch_file(url: str, dest: Path, on_progress=None, cancel=None) -> int:
    safe = urllib.parse.quote(url, safe=":/?&=%(),._-~+")
    request = urllib.request.Request(
        safe, headers={**ZVYKI_HEADERS, "Referer": ZVYKI_BASE + "/"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        total = response.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        try:
            with dest.open("wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    return done


def _to_m4a(src: Path, dest: Path, title: str, artist: str) -> bool:
    """Zvyki отдаёт голый AAC — упаковываем в m4a без перекодирования (быстро)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
        "-c", "copy",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size < 10_000:
        log.warning("Zvyki: ffmpeg не упаковал файл: %s", result.stderr[-300:])
        return False
    return True


def _sniff_audio(head: bytes) -> str:
    """Каким форматом реально отдал файл сервер: 'mp3', 'aac' или пусто."""
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        # ADTS (AAC): syncword + layer == 00
        if (head[1] & 0xF6) == 0xF0:
            return "aac"
        # MPEG audio (mp3): syncword + layer != 00
        if (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0:
            return "mp3"
    return ""


def _zvyki_download_sync(
    track: Track, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    out_dir.mkdir(parents=True, exist_ok=True)
    if track.uploader:
        name = _safe_track_name(f"{track.uploader} - {track.title}")
    else:
        name = _safe_track_name(track.title)

    # Zvyki отдаёт то mp3, то голый AAC — определяем по первым байтам
    raw = out_dir / f"{name}.tmp"
    size = _zvyki_fetch_file(track.url, raw, on_progress, cancel)
    if cancel is not None and cancel.is_set():
        raw.unlink(missing_ok=True)
        raise DownloadCancelled()
    if size < 20_000:
        raw.unlink(missing_ok=True)
        raise DownloadError("Zvyki отдал пустой файл. Попробуйте другой трек.")
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raw.unlink(missing_ok=True)
        raise DownloadError(f"Файл больше {MAX_UPLOAD_MB} МБ — Telegram не примет.")

    with raw.open("rb") as fh:
        kind = _sniff_audio(fh.read(4))

    if kind == "mp3":
        path = out_dir / f"{name}.mp3"
        raw.replace(path)
    else:
        m4a = out_dir / f"{name}.m4a"
        if _to_m4a(raw, m4a, track.title, track.uploader):
            raw.unlink(missing_ok=True)
            path = m4a
        else:
            # без ffmpeg отдадим как документ, но трек не потеряем
            path = out_dir / f"{name}.aac"
            raw.replace(path)

    return path, {
        "title": track.title,
        "track": track.title,
        "artist": track.uploader,
        "uploader": track.uploader,
        "duration": track.duration,
    }


def _zvyki_from_url_sync(
    url: str, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    """Ссылка на Zvyki из чата: страница трека или прямой файл data.zvyki.com."""
    if (urlsplit(url).netloc or "").lower().startswith("data."):
        stem = urllib.parse.unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        title = re.sub(r"\.(?:mp3|aac|m4a)$", "", stem, flags=re.I)
        title = title.replace("(Zvyki.com)", "").replace("_", " ").strip() or "Zvyki"
        artist = ""
        if " - " in title:
            artist, _, title = title.partition(" - ")
        track = Track(
            provider="zv",
            video_id=url,
            url=url,
            title=title.strip() or "Zvyki",
            uploader=artist.strip(),
            duration=0,
        )
    else:
        track = _zvyki_song_track_sync(url)
    return _zvyki_download_sync(track, out_dir, on_progress, cancel)


def is_youtube(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    return (
        host.endswith("youtube.com")
        or host.endswith("youtu.be")
        or host.endswith("youtube-nocookie.com")
    )


def _yt_bot_check(exc: Exception) -> bool:
    """YouTube просит «подтвердите, что вы не робот»."""
    low = str(exc).lower()
    return (
        "sign in to confirm" in low
        or "not a bot" in low
        or "confirm you" in low
    )


YOUTUBE_BOT_CHECK = (
    "YouTube не пустил сервер без входа.\n"
    "Обновите COOKIES_FILE с cookies YouTube и выполните /yt_debug "
    "(или включите COBALT_YOUTUBE=1 с рабочим COBALT_API)."
)

# Совместимость со старыми сборками: прежний inline.py ещё импортирует это имя.
# Актуальный код его не использует — YouTube включён.
YOUTUBE_DISABLED = YOUTUBE_BOT_CHECK


# ---------------------------------------------------------------- cobalt
# cobold (tskau/cobold) не качает YouTube сам: он отправляет ссылку в cobalt API,
# получает tunnel/redirect/picker и пересылает файл в Telegram. Здесь тот же
# принцип: инстансы перебираются по очереди, ошибки переводятся на русский.
# Публичные инстансы YouTube обычно не отдают (нужен ключ/свой сервер), зато
# тот же клиент пригодится для остальных сервисов cobalt.


class _CobaltError(Exception):
    """Ответ cobalt со статусом error (или сетевой сбой)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _CobaltTooBig(Exception):
    """Файл больше лимита Telegram — надо пробовать качество пониже."""


_COBALT_MESSAGES = {
    "error.api.missing": (
        "cobalt не настроен: добавьте COBALT_API в переменные окружения."
    ),
    "error.api.connection": "cobalt не отвечает — проверьте адрес в COBALT_API.",
    "error.api.bad_response": "cobalt ответил непонятно — возможно, это не cobalt.",
    "error.api.auth.jwt.missing": (
        "инстанс требует токен turnstile — нужен свой инстанс или ключ от владельца."
    ),
    "error.api.auth.api_key.missing": "инстанс требует API-ключ.",
    "error.api.auth.key.invalid": "инстанс не принял API-ключ.",
    "error.api.rate_exceeded": "инстанс ограничил частоту запросов — подождите минуту.",
    "error.api.youtube.login": "YouTube требует вход в аккаунт.",
    "error.api.youtube.no_session_tokens": "YouTube требует сессию/PO-токен.",
    "error.api.content.video.age": (
        "видео с возрастным ограничением — нужны cookies аккаунта YouTube."
    ),
    "error.api.content.video.region": "видео заблокировано в регионе инстанса.",
    "error.api.content.video.unavailable": "видео недоступно (удалено или скрыто).",
    "error.api.service.unsupported": "этот инстанс не умеет YouTube.",
}

# от лучшего к худшему: если файл не влезает в лимит — понижаем качество
_COBALT_LADDER = ("max", "2160", "1440", "1080", "720", "480", "360", "240", "144")

# Сервисы, которые умеет cobalt, и их домены. Ключи совпадают с именами
# сервисов в cobalt. Какие из них реально отдавать в cobalt, задаёт
# COBALT_SERVICES. YouTube включается отдельно — через COBALT_YOUTUBE.
_COBALT_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com", "instagr.am", "ddinstagram.com"),
    "reddit": ("reddit.com", "redd.it", "redditmedia.com"),
    "rutube": ("rutube.ru",),
    "tiktok": ("tiktok.com", "tt.site"),
    "twitter": ("twitter.com", "x.com"),
    "soundcloud": ("soundcloud.com", "snd.sc"),
    "facebook": ("facebook.com", "fb.watch"),
    "vimeo": ("vimeo.com",),
    "twitch": ("twitch.tv",),
    "dailymotion": ("dailymotion.com", "dai.ly"),
    "vk": ("vk.com", "vk.ru", "vkvideo.ru", "vk.cc", "vk.link"),
    "ok": ("ok.ru",),
    "pinterest": ("pinterest.com", "pin.it"),
    "tumblr": ("tumblr.com",),
    "snapchat": ("snapchat.com",),
    "streamable": ("streamable.com",),
    "newgrounds": ("newgrounds.com",),
    "bilibili": ("bilibili.com", "b23.tv"),
    "bluesky": ("bsky.app",),
    "loom": ("loom.com",),
}


_COBALT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def _cobalt_instances() -> list[tuple[str, str]]:
    """Разбирает COBALT_API: url или url|auth, по одному в строке/через запятую."""
    instances: list[tuple[str, str]] = []
    raw = (COBALT_API or "").replace("\\n", "\n")
    for chunk in re.split(r"[\n,]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        url, _, auth = chunk.partition("|")
        url = url.strip().rstrip("/")
        if not url.startswith("http"):
            continue
        instances.append((url, auth.strip()))
    return instances


def _cobalt_auth(auth: str) -> str:
    """cobold передаёт auth как есть; голый ключ считаем Api-Key."""
    return auth if " " in auth else f"Api-Key {auth}"


def _cobalt_services() -> set[str]:
    """Список сервисов из COBALT_SERVICES (YouTube задаётся отдельно).

    ``all``/``*`` — все сервисы, которые умеет cobalt, кроме YouTube.
    ``none``/``off`` — не отдавать в cobalt ничего (только yt-dlp).
    """
    raw = (COBALT_SERVICES or "").replace("\\n", ",")
    items = {chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()}
    if not items or items & {"none", "off", "0", "no"}:
        return set()
    if items & {"all", "*"}:
        return {name for name in _COBALT_HOSTS if name != "youtube"}
    return items


def _cobalt_service(url: str) -> str | None:
    """Имя сервиса cobalt, если ссылку стоит сначала отдать в cobalt."""
    host = (urlsplit(url).netloc or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    enabled = _cobalt_services()
    for name, domains in _COBALT_HOSTS.items():
        if name == "youtube":
            # YouTube по умолчанию идёт напрямую в yt-dlp: cookies из
            # COOKIES_FILE надёжнее, чем заведомо неудачный запрос в cobalt.
            if not COBALT_YOUTUBE:
                continue
        elif name not in enabled:
            continue
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return None


def _use_cobalt(url: str) -> bool:
    """Сначала cobalt: YouTube — при COBALT_YOUTUBE=1, остальные — из COBALT_SERVICES."""
    if not _cobalt_instances():
        return False
    return _cobalt_service(url) is not None


# Подписи для кнопки «Сервисы»: ключи совпадают с _COBALT_HOSTS / cobalt.
_COBALT_LABELS = {
    "bilibili": "Bilibili",
    "bluesky": "Bluesky",
    "dailymotion": "Dailymotion",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "loom": "Loom",
    "newgrounds": "Newgrounds",
    "ok": "OK.ru",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "rutube": "Rutube",
    "snapchat": "Snapchat",
    "soundcloud": "SoundCloud",
    "streamable": "Streamable",
    "tiktok": "TikTok",
    "tumblr": "Tumblr",
    "twitch": "Twitch (клипы)",
    "twitter": "X (Twitter)",
    "vimeo": "Vimeo",
    "vk": "VK",
    "youtube": "YouTube",
}

# cobalt в списке сервисов пишет «twitch clips», а в запросах — «twitch».
_COBALT_ALIASES = {"twitch clips": "twitch", "twitchclips": "twitch"}
_COBALT_FALLBACK_SERVICES = [name for name in _COBALT_HOSTS if name != "youtube"]


def _cobalt_key(name: str) -> str:
    lowered = " ".join(name.lower().split())
    return _COBALT_ALIASES.get(lowered, lowered)


def _cobalt_live_services() -> tuple[list[str], str, str]:
    """Спрашивает у инстанса GET /: какие сервисы он реально умеет.

    Возвращает (сервисы, версия, адрес инстанса). Если ни один инстанс не
    ответил, отдаёт встроенный список — кнопка должна работать всегда.
    """
    for base, _auth in _cobalt_instances():
        try:
            request = urllib.request.Request(
                f"{base}/",
                headers={"Accept": "application/json", "User-Agent": _COBALT_UA},
            )
            with _cobalt_open(request, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log.debug("cobalt: список сервисов не получен с %s: %s", base, exc)
            continue
        cobalt = data.get("cobalt") if isinstance(data, dict) else None
        if not isinstance(cobalt, dict):
            continue
        raw = cobalt.get("services")
        if not isinstance(raw, list):
            continue
        services = []
        for item in raw:
            key = _cobalt_key(str(item))
            if key and key not in services:
                services.append(key)
        if services:
            return services, str(cobalt.get("version") or ""), base

    return list(_COBALT_FALLBACK_SERVICES), "", ""


def cobalt_services_report() -> str:
    """Текст для кнопки «Сервисы»: что умеет cobalt и что включено в боте."""
    enabled = _cobalt_services()
    services, version, base = _cobalt_live_services()

    lines = ["🎛 <b>Сервисы cobalt</b>"]
    if version:
        lines.append(f"<i>инстанс v{html.escape(version)}</i>")
    lines.append("")

    if enabled:
        pretty = ", ".join(_COBALT_LABELS.get(name, name.title()) for name in sorted(enabled))
        lines.append(f"⚡ Через cobalt: <b>{pretty}</b>")
    else:
        lines.append("⚡ Через cobalt сейчас ничего не включено.")
    if COBALT_YOUTUBE:
        lines.append("▶️ YouTube тоже отдаётся в cobalt (COBALT_YOUTUBE=1).")
    else:
        lines.append("▶️ YouTube качается напрямую: yt-dlp + cookies.")
    lines.append("")

    lines.append(f"Доступно у инстанса: <b>{len(services)}</b>")
    if not base:
        lines.append(
            "<i>COBALT_API не задан — показан встроенный список сервисов.</i>"
        )
    for name in services:
        mark = "✅" if name in enabled else "▫️"
        label = _COBALT_LABELS.get(name, name.title())
        lines.append(f"{mark} {html.escape(label)}")
    lines.append("")

    unknown = sorted(enabled - set(services) - {"youtube"})
    if unknown:
        lines.append(
            "⚠️ Включено в COBALT_SERVICES, но инстанс не умеет: "
            + ", ".join(html.escape(name) for name in unknown)
        )
        lines.append("")

    lines.append(
        "<i>✅ — бот отдаёт ссылку в cobalt, ▫️ — качает своим путём "
        "(yt-dlp или собственный разбор).</i>"
    )
    return "\n".join(lines)


def _cobalt_open(request: urllib.request.Request, timeout: int):
    if COBALT_PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": COBALT_PROXY, "https": COBALT_PROXY})
        )
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _cobalt_post(base: str, auth: str, payload: dict, timeout: int = 120) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _COBALT_UA,
    }
    if auth:
        headers["Authorization"] = _cobalt_auth(auth)
    request = urllib.request.Request(
        f"{base}/",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    status = 0
    try:
        with _cobalt_open(request, timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as exc:  # noqa: BLE001
        raise _CobaltError(f"error.api.connection: {exc}") from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise _CobaltError(f"error.api.bad_response: HTTP {status}") from exc

    if not isinstance(data, dict):
        raise _CobaltError("error.api.bad_response")
    if data.get("status") == "error":
        error = data.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        raise _CobaltError(str(code or "error.api.unknown"))
    return data


def _cobalt_payload(url: str, mode: str, quality: str) -> dict:
    if mode == "audio":
        return {
            "url": url,
            "downloadMode": "audio",
            "filenameStyle": "basic",
            "audioFormat": "mp3",
            "audioBitrate": "320",
        }
    payload = {
        "url": url,
        "downloadMode": "auto",
        "filenameStyle": "basic",
        "videoQuality": quality,
    }
    if is_youtube(url):
        payload |= {
            "youtubeVideoCodec": "h264",
            "youtubeVideoContainer": "mp4",
            "youtubeBetterAudio": True,
        }
    return payload


def _cobalt_pick(data: dict) -> tuple[str, str]:
    """Достаёт из ответа ссылку на файл и имя (tunnel/redirect/picker/…)."""
    status = data.get("status")

    if status == "picker":
        items = [i for i in (data.get("picker") or []) if isinstance(i, dict) and i.get("url")]
        if not items:
            raise _CobaltError("error.api.content.video.unavailable")
        item = next((i for i in items if i.get("type") == "video"), items[0])
        return str(item["url"]), str(item.get("filename") or "")

    if status == "local-processing":
        tunnels = [t for t in (data.get("tunnel") or []) if isinstance(t, str)]
        if not tunnels:
            raise _CobaltError("error.api.content.video.unavailable")
        output = data.get("output") or {}
        return tunnels[0], str(output.get("filename") or "")

    if status in ("tunnel", "redirect"):
        url = data.get("url")
        if not url:
            raise _CobaltError("error.api.content.video.unavailable")
        return str(url), str(data.get("filename") or "")

    raise _CobaltError("error.api.bad_response")


def _cobalt_filename(raw: str, fallback: str) -> str:
    name = urllib.parse.unquote(raw or "")
    name = name.split("?", 1)[0].rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    if not name:
        return fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = (stem.strip(" .") or fallback.rsplit(".", 1)[0])[:80].strip(" .")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:5].lower()
    if not ext and "." in fallback:
        ext = fallback.rsplit(".", 1)[-1]
    return f"{stem}.{ext}" if ext else stem


def _cobalt_fetch(
    file_url: str,
    out_dir: Path,
    filename: str,
    auth: str,
    budget: int,
    on_progress=None,
    cancel=None,
) -> Path:
    headers = {"User-Agent": _COBALT_UA, "Accept": "*/*"}
    if auth:
        headers["Authorization"] = _cobalt_auth(auth)
    request = urllib.request.Request(file_url, headers=headers)

    with _cobalt_open(request, timeout=180) as response:
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype or "application/json" in ctype:
            raise _CobaltError("error.api.bad_response")

        total = int(response.headers.get("Content-Length") or 0)
        if total > budget:
            raise _CobaltTooBig()

        dest = out_dir / _cobalt_filename(filename, "media.mp4")
        done = 0
        try:
            with dest.open("wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > budget:
                        raise _CobaltTooBig()
                    handle.write(chunk)
                    if on_progress:
                        on_progress(done, total or None)
        except (_CobaltTooBig, DownloadCancelled):
            dest.unlink(missing_ok=True)
            raise
        except Exception:
            dest.unlink(missing_ok=True)
            raise

    if not dest.exists() or dest.stat().st_size == 0:
        raise _CobaltError("error.api.bad_response")
    return dest


def _cobalt_ladder(mode: str) -> list[str]:
    if mode == "audio":
        return ["1080"]  # качество не важно: cobalt отдаёт mp3
    start = (COBALT_QUALITY or "1080").strip().lower()
    if start not in _COBALT_LADDER:
        start = "1080"
    return list(_COBALT_LADDER[_COBALT_LADDER.index(start):])


def _cobalt_info(path: Path, url: str, data: dict) -> dict:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    meta = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
    return {
        "title": str(meta.get("title") or path.stem),
        "uploader": str(meta.get("artist") or ""),
        "duration": 0,
        "webpage_url": url,
    }


_COBALT_HINTS = (
    ("content.post.private", "пост приватный — инстансу нужны cookies."),
    (".private", "контент приватный — инстансу нужны cookies."),
    ("content.post.age", "контент 18+ — инстансу нужны cookies."),
    (".age", "контент 18+ — инстансу нужны cookies."),
    ("content.video.region", "заблокировано в регионе инстанса."),
    (".region", "заблокировано в регионе инстанса."),
    ("content.too_long", "слишком длинное видео для инстанса."),
    ("fetch.short_link", "не удалось раскрыть короткую ссылку."),
    (".live", "прямые трансляции не поддерживаются."),
    ("service.unsupported", "этот инстанс не умеет такой сервис."),
    ("rate_exceeded", "инстанс ограничил частоту запросов — подождите минуту."),
    (".login", "сервис требует вход — инстансу нужны cookies аккаунта."),
)


def _cobalt_message(exc: Exception | None, url: str | None = None) -> str:
    prefix = f"{service_name(url)}: " if url else ""
    if isinstance(exc, _CobaltTooBig):
        return (
            prefix
            + f"файл больше {MAX_UPLOAD_MB} МБ даже в низком качестве.\n"
            "Пришлите ссылку покороче или выберите «Только звук»."
        )
    if isinstance(exc, _CobaltError):
        base = exc.code.split(":", 1)[0].strip()
        text = _COBALT_MESSAGES.get(base)
        if not text and base.startswith("error.api.connection"):
            text = _COBALT_MESSAGES["error.api.connection"]
        if not text:
            for marker, hint in _COBALT_HINTS:
                if marker in base:
                    text = hint
                    break
        return prefix + (text or f"cobalt вернул ошибку ({exc.code})")
    if exc is None:
        return prefix + "cobalt не смог скачать файл."
    return prefix + f"cobalt не смог скачать ({str(exc)[:140]})."


def _clear_dir(out_dir: Path) -> None:
    """Убирает недокачанные файлы, чтобы следующая попытка шла с чистого листа."""
    try:
        for item in out_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
    except OSError:
        pass


def _cobalt_download_sync(
    url: str, out_dir: Path, mode: str = "auto", on_progress=None, cancel=None
) -> tuple[Path, dict]:
    instances = _cobalt_instances()
    if not instances:
        raise DownloadError(_cobalt_message(_CobaltError("error.api.missing"), url))

    out_dir.mkdir(parents=True, exist_ok=True)
    budget = MAX_UPLOAD_MB * 1024 * 1024
    fallback = "media.mp3" if mode == "audio" else "media.mp4"
    last: Exception | None = None

    for base, auth in instances:
        for quality in _cobalt_ladder(mode):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            try:
                response = _cobalt_post(base, auth, _cobalt_payload(url, mode, quality))
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                file_url, filename = _cobalt_pick(response)
                path = _cobalt_fetch(
                    file_url,
                    out_dir,
                    filename or fallback,
                    auth,
                    budget,
                    on_progress,
                    cancel,
                )
                info = _cobalt_info(path, url, response)
                log.info("cobalt: %s → %s (%s, %s)", base, path.name, mode, quality)
                return path, info
            except DownloadCancelled:
                _clear_dir(out_dir)
                raise
            except _CobaltTooBig:
                last = _CobaltTooBig()
                log.info("cobalt: %s не влез в лимит на качестве %s", base, quality)
                _clear_dir(out_dir)
                continue
            except _CobaltError as exc:
                last = exc
                log.warning("cobalt: %s (%s): %s", base, quality, exc.code)
                _clear_dir(out_dir)
                break  # ключ/контент — качество не поможет, сразу следующий инстанс
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("cobalt: %s: %s", base, exc)
                _clear_dir(out_dir)
                break

    raise DownloadError(_cobalt_message(last, url))


# Слова-маркеры «не оригинала»: каверы, ремиксы и ускорения не должны лезть
# вперёд, но и не выбрасываются — вдруг человеку нужен именно ремикс.
_VARIANT_HINTS = (
    "cover",
    "кавер",
    "remix",
    "ремикс",
    "rmx",
    "type beat",
    "speed up",
    "sped up",
    "slowed",
    "reverb",
    "nightcore",
    "bassboost",
    "bass boost",
    "hardstyle",
    "mashup",
    "gachi",
    "8d",
    "tiktok",
    "phonk",
    "drill",
)


_QUERY_DASH_RE = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")


def _query_words(query: str) -> list[str]:
    return [w for w in re.findall(r"[0-9a-zа-яё]+", query.lower()) if len(w) > 2]


def _split_query(query: str) -> tuple[str, str] | None:
    """«фотографирую закат - fem.love» → («фотографирую закат», «fem.love»).

    Тире с пробелами читаем как разделитель «автор — название». Порядок не
    угадываем: в разметке треков встречается и тот, и другой.
    """
    parts = [p.strip() for p in _QUERY_DASH_RE.split(query) if p.strip()]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _rank_tracks(query: str, tracks: list[Track]) -> list[Track]:
    """Оригиналы выше каверов; при «A - B» проверяем оба порядка автор/название."""
    words = _query_words(query)
    if not words:
        return tracks

    query_low = query.lower()
    penalize = [hint for hint in _VARIANT_HINTS if hint not in query_low]
    parts = _split_query(query)

    def score(track: Track) -> int:
        artist = (track.uploader or "").lower()
        title = track.title.lower()
        text = f"{artist} {title}"

        def pair(artist_side: str, title_side: str) -> int:
            value = 0
            for word in _query_words(artist_side):
                if word in artist:
                    value += 3
                elif word in title:
                    value += 1
            for word in _query_words(title_side):
                if word in title:
                    value += 3
                elif word in artist:
                    value += 1
            return value

        if parts:
            value = max(pair(parts[0], parts[1]), pair(parts[1], parts[0]))
        else:
            value = 3 * sum(1 for word in words if word in artist)
            value += 2 * sum(1 for word in words if word in title)

        for hint in penalize:
            if hint in text:
                value -= 4
        if track.provider == "hm":
            value += 1
        return value

    return sorted(tracks, key=score, reverse=True)


def _collect_search(tasks: list[asyncio.Task]) -> list[Track]:
    """Забирает готовые результаты, а незавершённые отпускает в фон."""
    tracks: list[Track] = []
    for task in tasks:
        if task.done():
            if task.cancelled() or task.exception() is not None:
                continue
            tracks.extend(task.result())
        else:
            # Не оставляем «exception was never retrieved», когда задача
            # доработает уже после ответа inline-клиенту.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return tracks


async def search(query: str, limit: int = 12, budget: float | None = None) -> list[Track]:
    """Ищет треки: Hitmo и Zvyki (готовые файлы), затем SoundCloud и YouTube.

    ``budget`` — сколько секунд ждать (для inline-режима). В inline все
    источники стартуют параллельно: Telegram ждёт ответ всего ~10 секунд,
    и последовательный Hitmo просто не оставлял времени остальным.
    """
    deadline = time.monotonic() + budget if budget else None

    # «A - B» отправляем поисковикам без тире: они лучше понимают набор слов,
    # а к какому полю относится каждая часть, разберёмся при ранжировании.
    parts = _split_query(query)
    query_for_search = " ".join(parts) if parts else query

    if deadline is not None:
        # Inline: Hitmo, Zvyki, SoundCloud и YouTube одновременно. Что успело
        # за отведённое время — отдаём, остальное продолжает догружаться в фоне.
        tasks = [
            asyncio.create_task(_search_one(query_for_search, provider, limit))
            for provider in ("hm", "zv", "sc", "yt")
        ]
        wait_time = max(0.5, deadline - time.monotonic())
        await asyncio.wait(set(tasks), timeout=wait_time)
        tracks = _collect_search(tasks)
    else:
        hm_timeout = 12.0
        try:
            tracks = await asyncio.wait_for(
                _search_one(query_for_search, "hm", min(limit, 30)),
                timeout=hm_timeout,
            )
        except Exception:  # noqa: BLE001 — таймаут или сбой Hitmo
            tracks = []

        # Hitmo редко отдаёт больше десятка треков — добираем Zvyki, SoundCloud
        # и YouTube, иначе список обрывается на нескольких результатах.
        if len(tracks) < limit:
            needed = max(4, limit - len(tracks))
            tasks = [
                asyncio.create_task(_search_one(query_for_search, provider, needed))
                for provider in ("zv", "sc", "yt")
            ]
            wait_for_extra = 8.0 if tracks else 20.0
            await asyncio.wait(set(tasks), timeout=wait_for_extra)
            tracks.extend(_collect_search(tasks))

    unique: list[Track] = []
    seen: set[str] = set()
    for track in tracks:
        key = f"{track.uploader} {track.title}".lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)

    return _rank_tracks(query, unique)[:limit]


# ---------------------------------------------------------------- скачивание


class _TooBig(Exception):
    """Файл превысил лимит Telegram во время скачивания."""


def _progress_hook(on_progress, limit: int | None = None, cancel=None):
    """Мост между потоком yt-dlp и прогресс-баром в сообщении."""
    if on_progress is None and limit is None and cancel is None:
        return []

    def hook(data: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            done = int(data.get("downloaded_bytes") or 0)
            if limit and done > limit:
                raise _TooBig()
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            if on_progress:
                on_progress(done, int(total) if total else None)
        elif status == "finished":
            done = int(data.get("downloaded_bytes") or data.get("total_bytes") or 0)
            if limit and done > limit:
                raise _TooBig()
            if on_progress:
                total = int(data.get("total_bytes") or done) or None
                on_progress(done, total)

    return [hook]


def _download_audio_sync(
    track: Track, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()

    cobalt_error: str | None = None
    target = _public_url(track.url)
    if track.provider == "yt":
        # YouTube нормализуем на youtu.be. В cobalt отправляем только при
        # COBALT_YOUTUBE=1, иначе сразу yt-dlp с cookies из COOKIES_FILE.
        track = Track(
            provider="yt",
            video_id=track.video_id,
            url=target,
            title=track.title,
            uploader=track.uploader,
            duration=track.duration,
        )

    # Reddit: звук из родного видео берём из DASH, без входа в аккаунт.
    if _is_reddit(track.url):
        try:
            return _reddit_download_sync(track.url, out_dir, "audio", on_progress, cancel)
        except _RedditExternal as external:
            track = Track(
                provider="",
                video_id="",
                url=external.url,
                title=track.title,
                uploader=track.uploader,
                duration=track.duration,
            )
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            log.warning("Reddit (аудио) не осилил %s: %s", track.url, exc)

    if _cobalt_instances() and _use_cobalt(track.url):
        try:
            return _cobalt_download_sync(
                track.url, out_dir, "audio", on_progress, cancel
            )
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            cobalt_error = str(exc)
            log.warning("cobalt (аудио) не осилил %s: %s", track.url, exc)
            _clear_dir(out_dir)

    if track.provider == "hm":
        return _hitmo_download_sync(track, out_dir, on_progress, cancel)
    if track.provider == "zv":
        return _zvyki_download_sync(track, out_dir, on_progress, cancel)
    if _is_pinterest(track.url):
        return _pinterest_download_sync(
            track.url, out_dir, "audio", on_progress, cancel
        )
    if _is_hitmo(track.url):
        # ссылка на страницу трека из Hitmo: разбираем её и качаем mp3
        return _hitmo_from_url_sync(track.url, out_dir, on_progress, cancel)
    if _is_zvyki(track.url):
        return _zvyki_from_url_sync(track.url, out_dir, on_progress, cancel)

    url = _vk_url(track.url) if _is_vk(track.url) else track.url

    opts = _opts() | {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title).80s.%(ext)s"),
        "writethumbnail": True,
        "progress_hooks": _progress_hook(on_progress, None, cancel),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": True},
        ],
    }

    try:
        info = _extract_with_rescue(
            _public_url(url), opts, download=True, cancel=cancel
        )
    except DownloadCancelled:
        raise
    except Exception as exc:
        if cobalt_error and _yt_bot_check(exc):
            raise DownloadError(cobalt_error) from exc
        raise

    mp3_files = sorted(out_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if mp3_files:
        return mp3_files[0], info

    # ffmpeg мог отсутствовать — отдадим что есть, лишь бы это было аудио
    fallback = [p for p in out_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS]
    if fallback:
        return fallback[0], info

    raise DownloadError("Не удалось извлечь аудио (проверьте, установлен ли ffmpeg).")


async def download_audio(
    track: Track, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    try:
        return await asyncio.to_thread(
            _download_audio_sync, track, out_dir, on_progress, cancel
        )
    except (DownloadError, DownloadCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("audio download failed: %s", track.url)
        raise DownloadError(friendly_error(exc, track.url)) from exc


def _size_fits(info: dict, max_height: int, budget: int) -> bool | None:
    """True/False — влезет ли видео с таким ограничением по высоте. None — размер неизвестен."""
    has_video = False
    best_video = 0
    best_audio = 0

    for fmt in info.get("formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if not size:
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        height = fmt.get("height") or 0

        if vcodec and vcodec != "none":
            has_video = True
            if height and height > max_height:
                continue
            best_video = max(best_video, int(size))
        elif acodec and acodec != "none":
            best_audio = max(best_audio, int(size))

    if has_video and not best_video:
        return False  # все видеоформаты выше ограничения
    if not has_video and not best_audio:
        return None  # размеров нет — решаем по ходу скачивания
    return best_video + best_audio <= budget


def _format_candidates(info: dict, budget: int) -> list[str]:
    """Список форматов от лучшего к худшему.

    Для Telegram предпочитаем h264 + m4a: такой mp4 проигрывается везде.
    Фильтр ``height<=N`` отбрасывает форматы без метаданных о высоте (частый
    случай у TikTok), поэтому для сервисов с неизвестными размерами остаётся
    вариант без потолка.
    """
    checks = [(cap, _size_fits(info, cap, budget)) for cap in (1080, 720, 480, 360)]

    if all(fits is None for _, fits in checks):
        # размеры вообще неизвестны — не перебираем кап, режем по ходу скачивания
        return ["bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b", "b"]

    candidates: list[str] = []
    if checks[0][1] is not True:  # 1080p целиком не влезает — сразу с потолком
        for cap, fits in checks:
            if fits is False:
                continue  # не влезает
            candidates.append(
                f"bv*[height<={cap}][vcodec^=avc1]+ba[acodec^=mp4a]/"
                f"bv*[height<={cap}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={cap}]+ba/b[height<={cap}]/b"
            )
            if fits:  # нашли подходящий потолок — ниже смысла нет
                break

    candidates.append("bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b")
    candidates.append("b[height<=360]/b")
    candidates.append("b")  # последний шанс: любой формат, даже без метаданных о высоте
    return candidates


def _http_get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _guess_ext(url: str) -> str:
    path = urlsplit(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".m4v", ".mov"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".mp4" if "video" in urlsplit(url).netloc else ".jpg"


def _x_tweet_json(user: str, status_id: str) -> dict | None:
    """Публичный API без ключей: fxtwitter, запасной — vxtwitter."""
    for api in (
        f"https://api.fxtwitter.com/{user}/status/{status_id}",
        f"https://api.vxtwitter.com/{user}/status/{status_id}",
    ):
        try:
            data = json.loads(_http_get(api))
        except Exception as exc:  # noqa: BLE001
            log.debug("X API недоступен (%s): %s", api, exc)
            continue
        if isinstance(data, dict):
            tweet = data.get("tweet") if isinstance(data.get("tweet"), dict) else data
            if tweet:
                return tweet
    return None


def _x_media_sync(url: str, out_dir: Path) -> tuple[Path, dict] | None:
    """Картинки (и видео) из поста X — то, что yt-dlp качать не умеет."""
    match = X_STATUS_RE.search(url)
    if not match:
        return None

    user, status_id = match.group(1), match.group(2)
    tweet = _x_tweet_json(user, status_id)
    if not tweet:
        return None

    media = tweet.get("media") or {}
    urls: list[str] = []
    for photo in media.get("photos") or []:
        if photo.get("url"):
            urls.append(photo["url"])
    for video in media.get("videos") or []:
        if video.get("url"):
            urls.append(video["url"])
    for item in tweet.get("mediaURLs") or []:  # формат vxtwitter
        if isinstance(item, str) and item not in urls:
            urls.append(item)

    if not urls:
        return None

    text = " ".join((tweet.get("text") or "").split())
    author = (tweet.get("author") or {}).get("name") or tweet.get("user_name") or f"@{user}"
    budget = MAX_UPLOAD_MB * 1024 * 1024

    for media_url in urls:
        try:
            data = _http_get(media_url, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log.warning("X: не скачалось %s: %s", media_url, exc)
            continue
        if len(data) > budget:
            log.info("X: файл больше лимита (%s байт), пробуем следующий", len(data))
            continue

        path = out_dir / f"{status_id}{_guess_ext(media_url)}"
        path.write_bytes(data)
        return path, {
            "title": text[:120] or f"Пост @{user}",
            "uploader": author,
            "duration": 0,
        }

    return None


# ---------------------------------------------------------------- FurAffinity
# www.furaffinity.net держит всё за Cloudflare. Обычные работы он с серверного
# IP ещё отдаёт, а 18+ — только после входа: без сессии вместо картинки в
# HTML остаётся логотип сайта. Поэтому бот логинится сам (FA_LOGIN/FA_PASSWORD)
# и только потом ищет картинку. Если Cloudflare не пустил вообще — перебираем
# обходные пути: «ботовый» UA, прокси Google и Microsoft, веб-архив, FAExport.
FA_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FA_VIEW_RE = re.compile(
    r"https?://(?:[\w-]+\.)?furaffinity\.net/(?:view|full|artwork)/(\d+)", re.I
)
FA_FILE_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:furaffinity\.net|facdn\.net)/[^\s\"'<>]+?"
    r"\.(?:jpe?g|png|gif|webp)",
    re.I,
)
# Картинки работ лежат на d./t. (старые — d2./t2.facdn.net), а логотип и
# оформление — на www./static.: их принимать нельзя, иначе вместо работы
# уходит логотип FurAffinity.
FA_ART_HOSTS = ("d.", "t.", "d2.", "t2.")
FA_STATIC_MARKS = ("/themes/", "/img/", "/css/", "/js/")

FA_BLOCKED = (
    "FurAffinity не открылся: Cloudflare не пускает сервер.\n"
    "Пришлите прямую ссылку на картинку (d.furaffinity.net/…)."
)
FA_MATURE = (
    "FurAffinity не отдал картинку: работа помечена 18+, а такие он "
    "показывает только после входа в аккаунт.\n"
    "Вход настроен неверно — проверьте FA_LOGIN и FA_PASSWORD."
)

_FA_LOCK = threading.Lock()
_FA_STATE: dict = {"opener": None, "jar": None, "logged": False, "last_try": 0.0}
_FA_LOGIN_COOLDOWN = 600.0  # не долбим FA логином чаще раза в 10 минут


def _is_fa(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith(("furaffinity.net", "facdn.net"))


def _fa_is_art(url: str) -> bool:
    """Ссылка на файл работы, а не на логотип/оформление сайта."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not host.endswith((".furaffinity.net", ".facdn.net")):
        return False
    if not host.startswith(FA_ART_HOSTS):
        return False
    return not any(mark in parts.path.lower() for mark in FA_STATIC_MARKS)


def _fa_attr(tag: str, *names: str) -> str:
    """Значение атрибута из тега: порядок и кавычки бывают любыми."""
    for name in names:
        found = re.search(rf'\b{name}\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not found:
            continue
        value = html.unescape(found.group(1)).strip().replace("\\/", "/")
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http"):
            return value
    return ""


def _fa_page_image(body: str) -> str:
    """Ссылка на саму картинку из HTML страницы submission."""
    candidates: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", body, re.I):
        if "submissionimg" not in tag.lower():
            continue
        # data-fullview-src — оригинал, src — превью для страницы
        candidates.append(_fa_attr(tag, "data-fullview-src", "data-src", "src"))
    candidates.append(_meta_value(body, "og:image"))

    files = [url for url in candidates if url and FA_FILE_RE.match(url) and _fa_is_art(url)]
    for url in files:
        # t.furaffinity.net — превью 800 px, d. — оригинал: сначала оригинал
        if not urlsplit(url).netloc.lower().startswith("t"):
            return url
    return files[0] if files else ""


def _fa_title_author(body: str) -> tuple[str, str]:
    """«Название» и автор из <title>: `"Title" by artist -- Fur Affinity`."""
    title = _meta_value(body, "og:title")
    author = ""
    found = re.search(r"<title>\s*(.*?)\s*(?:--|—)\s*Fur Affinity", body, re.S | re.I)
    if found:
        text = re.sub(r"\s+", " ", found.group(1)).strip()
        parts = re.split(r"\s+by\s+", text, maxsplit=1)
        if len(parts) == 2:
            title = title or parts[0].strip()
            author = parts[1].strip()
        elif not title:
            title = text
    return title.strip().strip('"'), author.strip()


def _fa_cookie_header() -> str:
    """FA_COOKIE в виде заголовка: переносы строк и лишние `;` не мешают."""
    if not FA_COOKIE:
        return ""
    parts = [part.strip() for part in re.split(r"[;\n\r]+", FA_COOKIE) if part.strip()]
    return "; ".join(parts)


def _fa_headers(ua: str = FA_UA, **extra: str) -> dict[str, str]:
    headers = {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookie = _fa_cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    headers.update(extra)
    return headers


def _fa_opener():
    """Один opener на процесс: он и хранит cookies сессии FurAffinity."""
    with _FA_LOCK:
        if _FA_STATE["opener"] is None:
            jar = http.cookiejar.CookieJar()
            _FA_STATE["jar"] = jar
            _FA_STATE["opener"] = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
        return _FA_STATE["opener"]


def _fa_get(url: str, ua: str = FA_UA, timeout: float = 25, **extra) -> str:
    request = urllib.request.Request(url, headers=_fa_headers(ua, **extra))
    with _fa_opener().open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _fa_post(url: str, data: bytes, ua: str = FA_UA, timeout: float = 30) -> str:
    request = urllib.request.Request(
        url,
        data=data,
        headers=_fa_headers(
            ua,
            **{
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Referer": "https://www.furaffinity.net/login/",
                "Origin": "https://www.furaffinity.net",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
        ),
    )
    with _fa_opener().open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _fa_raw_attr(tag: str, name: str) -> str:
    """Значение атрибута как есть — для полей формы, где нет http-ссылок."""
    found = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return html.unescape(found.group(1)).strip() if found else ""


def _fa_login_payload(body: str) -> tuple[str, dict[str, str], str]:
    """Собираем форму входа из HTML: имена полей у FA меняются.

    Берём форму, в которой есть поле пароля, и переносим все её input'ы как
    есть (вместе со скрытым токеном), подставляя логин и пароль. Так бот не
    зависит от конкретных имён `loginname`/`loginpassword` и от `_csrf`.
    """
    for form in re.finditer(r"<form\b[^>]*>(.*?)</form>", body, re.S | re.I):
        inner = form.group(1)
        if not re.search(r"""type\s*=\s*["']password""", inner, re.I):
            continue
        opening = form.group(0)[: form.group(0).find(">") + 1]
        action = _fa_raw_attr(opening, "action") or "/login"
        target = urllib.parse.urljoin("https://www.furaffinity.net/login", action)

        fields: dict[str, str] = {}
        seen: list[str] = []
        user_field = pass_field = ""
        for inp in re.findall(r"<input\b[^>]*>", inner, re.I):
            name = _fa_raw_attr(inp, "name")
            if not name:
                continue
            kind = (_fa_raw_attr(inp, "type") or "text").lower()
            value = _fa_raw_attr(inp, "value")
            if kind == "password":
                pass_field = name
            elif kind in {"text", "email", "tel"} and not user_field:
                user_field = name
                fields[name] = FA_LOGIN
            elif kind in {"submit", "button", "file", "reset", "image"}:
                continue
            elif kind == "checkbox":
                if "checked" in inp.lower():
                    fields[name] = value or "1"
            elif value:
                fields[name] = value
            seen.append(f"{name}:{kind}")

        if not pass_field:
            continue
        if not user_field:
            user_field = "loginname"
            fields[user_field] = FA_LOGIN
        fields[pass_field] = FA_PASSWORD
        return target, fields, f"поля {', '.join(seen)} (всего {len(fields)})"
    return "", {}, "нет формы с полем пароля"


def _fa_http_detail(exc: Exception) -> str:
    """Короткое описание HTTP-ошибки: статус, важные заголовки и начало тела."""
    parts = [f"{type(exc).__name__}: {exc}"]
    headers = getattr(exc, "headers", None)
    if headers:
        for key in ("Server", "CF-Mitigated", "Retry-After", "Location"):
            value = headers.get(key)
            if value:
                parts.append(f"{key}={value}")
    try:
        raw = exc.read()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        raw = b""
    if raw:
        text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            parts.append("тело: " + text[:160])
    return " | ".join(parts)


def _fa_login(force: bool = False) -> tuple[bool, str]:
    """Вход в аккаунт: без него FA не показывает работы 18+.

    Возвращает (успех, пояснение) — пояснение уходит в сообщение об ошибке,
    чтобы было видно, на каком шаге всё сломалось.
    """
    if _fa_cookie_header():
        return False, (
            "задан FA_COOKIE, вход по паролю не нужен; если работа не "
            "открылась — cookies протухли, обновите a и b"
        )
    if not FA_LOGIN or not FA_PASSWORD:
        return False, "FA_LOGIN/FA_PASSWORD не заданы"

    with _FA_LOCK:
        if _FA_STATE["logged"] and not force:
            return True, "сессия уже открыта"
        now = time.monotonic()
        if not force and now - _FA_STATE["last_try"] < _FA_LOGIN_COOLDOWN:
            return False, "прошлая попытка была недавно, жду 10 минут"
        _FA_STATE["last_try"] = now

    try:
        page = _fa_get("https://www.furaffinity.net/login")
    except Exception as exc:  # noqa: BLE001
        log.warning("FurAffinity: страница входа не открылась: %s", exc)
        return False, f"страница входа: {type(exc).__name__}: {str(exc)[:90]}"

    target, fields, note = _fa_login_payload(page)
    if not fields:
        return False, f"форма входа не найдена ({note})"

    # FA иногда отвечает 503 на POST со слэшем: пробуем и вариант без него
    attempts = [target]
    if target.endswith("/"):
        attempts.append(target.rstrip("/"))

    body = ""
    detail = ""
    for attempt in attempts:
        try:
            body = _fa_post(attempt, urllib.parse.urlencode(fields).encode())
            detail = ""
            break
        except urllib.error.HTTPError as exc:
            detail = _fa_http_detail(exc)
            log.warning("FurAffinity: POST %s -> %s", attempt, detail)
            if exc.code not in (403, 503):
                break
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("FurAffinity: POST %s -> %s", attempt, detail)
            break

    if detail:
        return False, f"POST {attempts[0]}: {detail}"

    logged = "/logout" in body.lower() or "log out" in body.lower()
    with _FA_LOCK:
        _FA_STATE["logged"] = logged
    if logged:
        log.info("FurAffinity: вошли как %s (%s)", FA_LOGIN, note)
        return True, f"вошли как {FA_LOGIN} ({note})"

    hint = ""
    if re.search(r"invalid|incorrect|wrong password|try again", body, re.I):
        hint = ", в ответе ошибка входа"
    elif "just a moment" in body[:4000].lower():
        hint = ", POST упёрся в Cloudflare"
    log.warning("FurAffinity: FA не принял логин %s (в ответе нет /logout) %s", FA_LOGIN, hint)
    return False, f"FA не принял вход ({note}{hint})"


def _fa_page_meta(body: str) -> tuple[str, str, str] | None:
    """Разбор страницы submission; None — это не она (челлендж Cloudflare)."""
    head = body[:4000].lower()
    if "just a moment" in head or "attention required" in head:
        return None
    if "fur affinity" not in body[:30000].lower():
        return None
    title, author = _fa_title_author(body)
    return _fa_page_image(body), title, author


def _fa_fetch_page(sub_id: str) -> tuple[str, str, str] | None:
    """Ищем страницу submission там, где Cloudflare её ещё отдаёт."""
    direct = f"https://www.furaffinity.net/view/{sub_id}/"
    quoted = urllib.parse.quote(direct, safe="")
    sources = (
        (direct, FA_UA),
        (direct, "TelegramBot (like TwitterBot)"),
        (
            f"https://www-furaffinity-net.translate.goog/view/{sub_id}/"
            "?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en",
            FA_UA,
        ),
        (f"https://www.translatetheweb.com/?from=&to=en&a={quoted}", FA_UA),
        (f"https://web.archive.org/web/2025/{direct}", FA_UA),
    )

    partial: tuple[str, str, str] | None = None
    for source, user_agent in sources:
        try:
            body = _fa_get(source, user_agent)
        except Exception as exc:  # noqa: BLE001
            log.debug("FurAffinity: %s не открылся: %s", source[:70], exc)
            continue
        meta = _fa_page_meta(body)
        if meta is None:
            log.debug("FurAffinity: %s — не страница работы", source[:70])
            continue
        if meta[0]:
            log.info("FurAffinity: картинка найдена через %s", source[:70])
            return meta
        # страница открылась, но картинки нет — обычно это 18+ без входа
        if partial is None:
            partial = meta
        log.debug("FurAffinity: на %s картинки нет (%s байт)", source[:70], len(body))

    # FAExport — сторонний API поверх FurAffinity (тот же Cloudflare, но свой IP)
    try:
        data = json.loads(
            _fa_get(
                f"https://faexport.spangle.org.uk/submission/{sub_id}.json",
                timeout=20,
                **{"Accept": "application/json"},
            )
        )
        image = str(data.get("file_url") or "").strip()
        if image.startswith("http"):
            log.info("FurAffinity: картинка найдена через FAExport")
            return image, str(data.get("title") or ""), str(data.get("author") or "")
    except Exception as exc:  # noqa: BLE001
        log.debug("FurAffinity: FAExport не ответил: %s", exc)
    return partial


def _fa_image_ok(data: bytes) -> bool:
    """Настоящая картинка, а не HTML-заглушка Cloudflare."""
    if data[:2] == b"\xff\xd8":  # jpeg
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _fa_download(image_url: str) -> bytes:
    """Файл с CDN: без Referer и браузерных заголовков Cloudflare режет."""
    request = urllib.request.Request(
        image_url,
        headers=_fa_headers(
            Accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            Referer="https://www.furaffinity.net/",
        ),
    )
    with _fa_opener().open(request, timeout=90) as response:
        return response.read()


def _fa_from_url_sync(url: str, out_dir: Path, on_progress=None) -> tuple[Path, dict]:
    if _fa_is_art(url):
        image, title, author = url, "", ""
    else:
        match = FA_VIEW_RE.search(url)
        if not match:
            raise DownloadError(
                "Это FurAffinity, но не ссылка на картинку.\n"
                "Пришлите ссылку вида www.furaffinity.net/view/12345678/"
            )
        sub_id = match.group(1)
        found = _fa_fetch_page(sub_id)
        login_note = ""
        if not (found and found[0]) and _FA_STATE["logged"]:
            # думали, что сессия живая, а картинки нет — возможно, она протухла
            log.info("FurAffinity: сессия не дала картинку, вхожу заново")
            with _FA_LOCK:
                _FA_STATE["logged"] = False
                _FA_STATE["last_try"] = 0.0
        if not (found and found[0]):
            ok, login_note = _fa_login()
            if ok:
                log.info("FurAffinity: открываю работу под аккаунтом %s", FA_LOGIN)
                found = _fa_fetch_page(sub_id) or found
        if not found:
            raise DownloadError(FA_BLOCKED)
        image, title, author = found
        if not image:
            if _FA_STATE["logged"]:
                message = (
                    "FurAffinity не отдал картинку: вход выполнен, но работа 18+ "
                    "всё равно скрыта.\nВключите в настройках аккаунта показ "
                    "взрослого контента (Settings → Show Mature Content)."
                )
            elif _fa_cookie_header():
                message = (
                    "FurAffinity не отдал картинку: cookies FA_COOKIE не сработали.\n"
                    "Войдите в FA в браузере и обновите cookies a и b."
                )
            else:
                message = FA_MATURE
            if login_note and not _fa_cookie_header():
                message += f"\nВход: {login_note}"
            if "503" in login_note:
                message += (
                    "\nFA блокирует сам вход с сервера — задайте на Render "
                    "переменную FA_COOKIE (cookies a и b из браузера)."
                )
            raise DownloadError(message)

    try:
        data = _dns_retry(lambda: _fa_download(image))
    except Exception as exc:  # noqa: BLE001
        log.warning("FurAffinity: файл не скачался (%s): %s", image, exc)
        error = DownloadError(
            "FurAffinity не отдал картинку: Cloudflare блокирует серверный IP."
        )
        error.image_url = image  # handlers покажет превью силами Telegram
        raise error from exc

    if not _fa_image_ok(data):
        log.warning(
            "FurAffinity: вместо картинки пришло %s (%s байт): %s",
            "HTML" if b"<html" in data[:500].lower() else "неизвестный формат",
            len(data),
            image,
        )
        error = DownloadError(
            "FurAffinity отдал вместо картинки страницу Cloudflare."
        )
        error.image_url = image  # превью подтянет Telegram
        raise error

    budget = MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > budget:
        raise DownloadError(
            f"Картинка весит {len(data) / 1024 / 1024:.0f} МБ — "
            f"больше лимита Telegram ({MAX_UPLOAD_MB} МБ)."
        )

    ext = _guess_ext(image)
    if ext not in {".jpg", ".png", ".gif", ".webp"}:
        ext = ".jpg"
    path = out_dir / f"fa{ext}"
    path.write_bytes(data)
    return path, {
        "title": title or "FurAffinity",
        "uploader": author,
        "duration": 0,
    }


def _fa_cookie_names() -> str:
    jar = _FA_STATE.get("jar")
    if not jar:
        return "—"
    names = sorted({cookie.name for cookie in jar})
    return ", ".join(names) if names else "пусто"


def _fa_diagnose_sync(url: str) -> str:
    """Отчёт о том, что FurAffinity реально отдаёт серверу (для /fa_debug)."""
    match = FA_VIEW_RE.search(url)
    if not match:
        return "Это не ссылка на работу FurAffinity. Нужен вид /view/<id>/."
    sub_id = match.group(1)
    direct = f"https://www.furaffinity.net/view/{sub_id}/"

    lines = [
        f"URL: {direct}",
        f"Логин: {FA_LOGIN or '—'} | FA_COOKIE: {'задан' if FA_COOKIE else 'нет'}",
    ]

    body = ""
    for label, user_agent in (
        ("браузерный UA", FA_UA),
        ("TelegramBot", "TelegramBot (like TwitterBot)"),
    ):
        try:
            body = _fa_get(direct, user_agent)
            lines.append(f"Страница ({label}): {len(body)} байт")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Страница ({label}): {type(exc).__name__}: {str(exc)[:110]}")
            continue
        if body:
            break

    if not body:
        lines.append("Итог: страница не открылась — Cloudflare режет сервер.")
        return "\n".join(lines)

    low = body[:4000].lower()
    challenged = "just a moment" in low or "attention required" in low
    lines.append(f"Cloudflare-челлендж: {'ДА' if challenged else 'нет'}")
    lines.append(f"#submissionImg: {'есть' if 'submissionimg' in body.lower() else 'НЕТ'}")
    og = _meta_value(body, "og:image")
    if og:
        kind = "арт" if _fa_is_art(og) else "не арт (логотип/оформление)"
        lines.append(f"og:image: {og[:110]} [{kind}]")
    else:
        lines.append("og:image: —")
    marks = [mark for mark in ("mature", "adult", "18+", "restricted") if mark in body.lower()]
    lines.append(f"Маркеры 18+: {', '.join(marks) if marks else 'нет'}")
    title, author = _fa_title_author(body)
    lines.append(f"Название: {title or '—'} | автор: {author or '—'}")
    cookie_src = _fa_cookie_header()
    if cookie_src:
        names = [part.split("=", 1)[0].strip() for part in cookie_src.split(";") if "=" in part]
        lines.append("FA_COOKIE: " + ", ".join(names))
    jar = _FA_STATE.get("jar")
    jar_names = {cookie.name for cookie in jar} if jar else set()
    lines.append(f"Cookies из ответов FA: {_fa_cookie_names()}")
    has_a = "a" in jar_names or any(part.strip().startswith("a=") for part in cookie_src.split(";"))
    lines.append(f"Cookie a (признак входа): {'есть' if has_a else 'НЕТ'}")

    early_image = _fa_page_image(body)
    if early_image:
        lines.append(f"Картинка сразу: {early_image[:110]}")

    try:
        export = json.loads(
            _fa_get(
                f"https://faexport.spangle.org.uk/submission/{sub_id}.json",
                timeout=20,
                **{"Accept": "application/json"},
            )
        )
        file_url = str(export.get("file_url") or "").strip()
        if file_url:
            lines.append(f"FAExport: {file_url[:110]} [{'арт' if _fa_is_art(file_url) else 'не арт'}]")
        else:
            lines.append("FAExport: ответ без file_url")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAExport: {type(exc).__name__}: {str(exc)[:110]}")

    if cookie_src:
        lines.append("Вход по паролю: пропущен — используется FA_COOKIE")
        ok = False
    else:
        try:
            login_page = _fa_get("https://www.furaffinity.net/login")
            target, fields, note = _fa_login_payload(login_page)
            lines.append(f"Страница входа: {len(login_page)} байт, форма: {note}")
            if target:
                lines.append(f"Куда POST: {target}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Страница входа: {type(exc).__name__}: {str(exc)[:110]}")

        with _FA_LOCK:
            _FA_STATE["logged"] = False
            _FA_STATE["last_try"] = 0.0
        ok, note = _fa_login(force=True)
        lines.append(f"Вход: {'OK' if ok else 'НЕТ'} — {note}")
        lines.append(f"Cookies после входа: {_fa_cookie_names()}")

    final_image = early_image
    if ok or (cookie_src and not final_image):
        try:
            fresh = _fa_get(direct)
            lines.append(
                f"С аккаунтом: {len(fresh)} байт, "
                f"#submissionImg {'есть' if 'submissionimg' in fresh.lower() else 'НЕТ'}"
            )
            final_image = _fa_page_image(fresh) or final_image
            lines.append(f"Картинка: {final_image or 'НЕ найдена'}")
            found = sorted(
                {
                    item
                    for item in re.findall(r"https?://[^\s\"'<>]+", fresh)
                    if FA_FILE_RE.match(item) and _fa_is_art(item)
                }
            )
            if found:
                lines.append("Арт-ссылки на странице: " + ", ".join(found[:3]))
            if cookie_src and not final_image:
                lines.append(
                    "FA_COOKIE задан, но картинки нет: cookies протухли "
                    "или в аккаунте выключен показ Mature/Adult."
                )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"С аккаунтом: {type(exc).__name__}: {str(exc)[:110]}")

    lines.append("Итог: картинка " + ("найдена" if final_image else "не найдена"))
    return "\n".join(lines)


async def fa_diagnose(url: str) -> str:
    return await asyncio.to_thread(_fa_diagnose_sync, url)


def _cookie_report() -> str:
    """Кратко о cookies: размер файла и признаки входа (без самих значений)."""
    if not COOKIES_FILE:
        return "cookies: не заданы (COOKIES_FILE пуст)"
    path = Path(COOKIES_FILE)
    if not path.exists():
        return f"cookies: файла нет: {COOKIES_FILE}"
    try:
        text = path.read_text("utf-8", "replace")
    except OSError as exc:
        return f"cookies: не читаются: {exc}"
    rows: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            # так помечены HttpOnly-cookie — в них лежат SID/HSID/LOGIN_INFO
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        rows.append(line)
    yt_rows = [line for line in rows if "youtube" in line or "google" in line]
    names = set()
    for line in yt_rows:
        parts = line.split("\t")
        if len(parts) >= 6:
            names.add(parts[5])
    keys = [
        key
        for key in ("SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
                    "__Secure-1PSID", "__Secure-3PSID", "SIDCC")
        if key in names
    ]
    tail = f", ключи входа: {', '.join(keys)}" if keys else ", ключей входа не видно"
    return (
        f"cookies: {path.name}, {path.stat().st_size} байт, "
        f"всего строк: {len(rows)}, youtube/google: {len(yt_rows)}{tail}"
    )


def cookie_login_state() -> str:
    """Реально ли cookies вводят в аккаунт: YouTube перекидывает на вход или нет."""
    if not COOKIES_FILE:
        return "вход по cookies: cookies не заданы"
    path = Path(COOKIES_FILE)
    if not path.exists():
        return "вход по cookies: файла нет"
    try:
        jar = http.cookiejar.MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:  # noqa: BLE001
        return f"вход по cookies: файл не разобрать ({type(exc).__name__})"
    names = {cookie.name for cookie in jar}
    if not names & {"SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}:
        return "вход по cookies: НЕТ — в файле только анонимные cookie"

    # Спрашиваем у самого YouTube, узнаёт ли он аккаунт по этим cookies.
    # Обычная страница /account не годится: для неё нужны cookies домена
    # .google.com, а боту хватает youtube.com — получилось бы ложное «НЕТ».
    sid = ""
    for cookie in jar:
        if cookie.name in ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"):
            sid = cookie.value
            break
    if not sid:
        return "вход по cookies: НЕТ — нет SAPISID (файл неполный)"
    stamp = str(int(time.time()))
    digest = hashlib.sha1(
        f"{stamp} {sid} https://www.youtube.com".encode()
    ).hexdigest()
    client_version = "2.20260101.00.00"
    body = json.dumps(
        {"context": {"client": {
            "clientName": "WEB", "clientVersion": client_version,
            "hl": "en", "gl": "US",
        }}}
    ).encode()
    request = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/account/account_menu"
        "?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8&prettyPrint=false",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-YouTube-Client-Name": "1",
            "X-YouTube-Client-Version": client_version,
            "Origin": "https://www.youtube.com",
            "Authorization": f"SAPISIDHASH {stamp}_{digest}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
        },
    )
    text = ""
    if _yt_proxy():
        # Проверяем вход тем же путём, что качает yt-dlp: с IP Render
        # account_menu тоже может отдавать 429, а через WARP — 200.
        try:
            _, raw = _fetch_via_proxy(
                request.full_url,
                timeout=12,
                headers=dict(request.headers),
                data=body,
            )
            text = raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return (
                "вход по cookies: проверить не удалось через прокси "
                f"({type(exc).__name__})"
            )
    if not text:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        try:
            with opener.open(request, timeout=12) as response:
                text = response.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return f"вход по cookies: проверить не удалось ({type(exc).__name__})"
    if "accountName" in text:
        return "вход по cookies: есть"
    return (
        "вход по cookies: НЕТ — YouTube не узнал аккаунт, "
        "cookies протухли или неполные"
    )


def js_runtime_report() -> str:
    """Есть ли JS-движок и скрипт n-challenge.

    Без них YouTube отдаёт меньше форматов, а с cookies обычно не отдаёт ничего:
    ссылки на видео шифруются n-challenge, который надо решать на JS.
    """
    runtimes: list[str] = []
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            found = getattr(ydl, "_js_runtimes", {}) or {}
    except Exception:  # noqa: BLE001
        found = {}
    for runtime in found.values():
        info = getattr(runtime, "info", None)
        if info is None:
            continue
        mark = "" if getattr(info, "supported", True) else " (версия не поддерживается)"
        runtimes.append(f"{info.name} {info.version}{mark}")
    try:
        import yt_dlp_ejs  # noqa: F401
    except Exception:  # noqa: BLE001
        ejs = "нет"
    else:
        try:
            from importlib.metadata import version

            ejs = f"есть {version('yt-dlp-ejs')}"
        except Exception:  # noqa: BLE001
            ejs = "есть"
    return (
        f"JS-движок: {', '.join(runtimes) if runtimes else 'НЕТ'}; "
        f"скрипт n-challenge (yt-dlp-ejs): {ejs}"
    )


class _YtDiagLogger:
    """Собирает пояснения yt-dlp, которые обычно скрыты из-за no_warnings.

    Без этого при отказе видно только «Failed to extract any player response»,
    а настоящая причина (Sign in to confirm you're not a bot, Incomplete data,
    HTTP 403/429 и т.п.) отбрасывается как предупреждение.
    """

    _KEYS = (
        "playability", "sign in", "not a bot", "po token", "po_token",
        "player response", "incomplete data", "http error", "403", "429",
        "consent", "not available", "unavailable", "blocked", "requires",
        "failed to", "no video formats",
    )

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _push(self, message: object) -> None:
        text = " ".join(str(message or "").split())
        if not text:
            return
        low = text.lower()
        if low.startswith(("error:", "warning:")) or "po token cache" in low:
            return
        if any(key in low for key in self._KEYS) and text not in self.messages:
            self.messages.append(text[:240])

    def debug(self, message: object) -> None:
        self._push(message)

    info = debug

    def warning(self, message: object) -> None:
        self._push(message)

    def error(self, message: object) -> None:
        self._push(message)


_EGRESS_CACHE: str | None = None
_CURL_STATUS_MARK = "\n__HTTP_STATUS__"


def _proxy_curl_args(proxy: str) -> list[str]:
    """Аргументы curl для socks5/socks5h/http-прокси (без пароля в логах)."""
    rest = proxy.split("://", 1)[-1]
    creds = ""
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
    if proxy.lower().startswith("socks"):
        args = ["--socks5-hostname", rest]
        if creds:
            args += ["--proxy-user", creds]
        return args
    return ["--proxy", proxy]


def _fetch_via_proxy(
    url: str,
    timeout: int = 12,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, bytes]:
    """GET/POST через прокси из YT_PROXY/PROXY — тем же путём, что ходит yt-dlp.

    curl взят потому, что он умеет socks5h без PySocks: имя домена
    резолвит туннель, а не датацентровый DNS Render.
    """
    proxy = _yt_proxy()
    if not proxy:
        raise RuntimeError("прокси не задан")
    args = [
        "curl", "-sS", "--compressed", "--max-time", str(timeout),
        "-A", (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-w", _CURL_STATUS_MARK + "%{http_code}",
    ]
    for name, value in (headers or {}).items():
        if name.lower() not in {"user-agent", "content-length", "host", "accept-encoding"}:
            args += ["-H", f"{name}: {value}"]
    args += _proxy_curl_args(proxy)
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        args += ["-b", writable_cookie_copy(COOKIES_FILE)]
    if data is not None:
        args += ["--data-binary", "@-"]
    args.append(url)
    result = subprocess.run(
        args, input=data, capture_output=True, timeout=timeout + 5
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail[:160] or f"curl код {result.returncode}")
    marker = _CURL_STATUS_MARK.encode()
    index = result.stdout.rfind(marker)
    if index < 0:
        raise RuntimeError("curl не вернул код ответа")
    status = int(result.stdout[index + len(marker):].strip() or 0)
    return status, result.stdout[:index]


def _proxy_egress_report() -> str | None:
    """Внешний IP уже через прокси: видно, что туннель действительно работает."""
    if not _yt_proxy():
        return None
    try:
        status, body = _fetch_via_proxy("https://ipinfo.io/json", timeout=10)
        data = json.loads(body.decode("utf-8", "replace"))
        bits = [
            str(data[key]) for key in ("ip", "country", "org") if data.get(key)
        ]
        if not bits:
            return f"IP через прокси: HTTP {status}, пустой ответ"
        return "IP через прокси: " + " | ".join(bits)
    except Exception as exc:  # noqa: BLE001
        return f"IP через прокси: проверить не удалось ({type(exc).__name__})"


def _egress_report() -> str:
    """Внешний IP сервера: видно, ходит ли Render через IPv6 и чей это адрес."""
    global _EGRESS_CACHE
    if _EGRESS_CACHE is not None:
        return _EGRESS_CACHE
    headers = {"User-Agent": "Mozilla/5.0 (compatible; tg-media-bot)"}
    parts: list[str] = []
    for name, url in (
        ("IPv4", "https://api.ipify.org?format=json"),
        ("IPv6", "https://api64.ipify.org?format=json"),
    ):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
            parts.append(f"{name}: {data.get('ip') or '?'}")
        except Exception:  # noqa: BLE001
            parts.append(f"{name}: ?")
    try:
        request = urllib.request.Request(
            "https://ipinfo.io/json", headers=headers
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        extra = ", ".join(
            str(data[key]) for key in ("country", "org") if data.get(key)
        )
        if extra:
            parts.append(extra)
    except Exception:  # noqa: BLE001
        pass
    _EGRESS_CACHE = "IP сервера: " + " | ".join(parts)
    return _EGRESS_CACHE


def _yt_page_probe(video_id: str) -> str:
    """Что отдаёт YouTube на обычные страницы — напрямую или через прокси."""
    if not video_id:
        return "страницы YouTube: не удалось выделить id видео"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    handlers = []
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
            handlers.append(urllib.request.HTTPCookieProcessor(jar))
        except Exception:  # noqa: BLE001
            pass
    opener = urllib.request.build_opener(*handlers)
    via_proxy = bool(_yt_proxy())

    def fetch(url: str) -> tuple[int, str]:
        if via_proxy:
            status, body = _fetch_via_proxy(url, timeout=15)
            return status, body.decode("utf-8", "replace")
        request = urllib.request.Request(url, headers=headers)
        with opener.open(request, timeout=15) as response:
            return response.status, response.read(1_500_000).decode(
                "utf-8", "replace"
            )

    parts: list[str] = []
    for name, url in (
        ("watch", f"https://www.youtube.com/watch?v={video_id}"),
        ("embed", f"https://www.youtube.com/embed/{video_id}?html5=1"),
    ):
        try:
            status, page = fetch(url)
        except urllib.error.HTTPError as exc:
            parts.append(f"{name}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001
            parts.append(f"{name}: ошибка {type(exc).__name__}")
            continue
        flags: list[str] = []
        if "ytInitialPlayerResponse" in page:
            flags.append("player есть")
        if '"visitorData"' in page:
            flags.append("visitor есть")
        if '"LOGGED_IN":true' in page or '"isSignedIn":true' in page:
            flags.append("вход виден")
        low = page.lower()
        if "confirm you" in low and "not a bot" in low:
            flags.append("ТРЕБУЕТ «не робот»")
        match = re.search(
            r'"playabilityStatus":\{[^{}]*?"status":"([A-Z_]+)"', page
        )
        if match:
            flags.append(f"playability {match.group(1)}")
        detail = ", ".join(flags) or "без признаков плеера"
        parts.append(f"{name}: HTTP {status}, {len(page) // 1024} КБ, {detail}")
    prefix = "страницы YouTube (через прокси)" if via_proxy else "страницы YouTube"
    return prefix + ": " + " | ".join(parts)


def _yt_diagnose_sync(url: str) -> str:
    """Проверяет, какой player_client YouTube пускает с IP Render.

    Быстрый путь: пробуем рабочий клиент из памяти и список YT_PLAYER_CLIENT,
    останавливаемся на первом успехе — дорогие проверки (вход по cookies,
    внешний IP, страницы YouTube) нужны только если не ответил никто.
    """
    match = _YT_ID_RE.search(url)
    video_id = match.group(1) if match else ""
    js_report = js_runtime_report()
    lines = [
        f"yt-dlp: {yt_dlp.version.__version__}",
        _cookie_report(),
        js_report,
        f"YT_PLAYER_CLIENT: {YT_PLAYER_CLIENT or '—'}",
        f"POT_SERVER: {'есть' if POT_SERVER else 'нет'}",
        f"PROXY: {'есть' if PROXY else 'нет'}",
        f"YT_PROXY: {'есть' if YT_PROXY else 'нет'}",
        f"рабочий клиент в памяти: {_yt_client or '—'}",
        "",
    ]
    clients: list[str] = []
    if _yt_client:
        clients.append(_yt_client)
    for chunk in (YT_PLAYER_CLIENT or "").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in clients:
            clients.append(chunk)
    for client in _YT_CLIENT_LADDER:
        if client not in clients:
            clients.append(client)
    ok_clients: list[str] = []
    seen_notes: set[str] = set()

    def try_client(client: str, extra: dict | None = None, label: str = "") -> bool:
        logger = _YtDiagLogger()
        opts = _yt_client_opts(client) | {
            "skip_download": True,
            "socket_timeout": 8,
            "extractor_retries": 0,
            "retries": 1,
            "verbose": True,
            "logger": logger,
        }
        if extra:
            opts.update(extra)
        name = f"{client}{label}"
        try:
            info = _extract_info(url, opts)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).replace("\n", " ")
            lines.append(f"{name}: ошибка — {message[:120]}")
            shown = 0
            for note in logger.messages:
                key = note[:80]
                if key in seen_notes:
                    continue
                seen_notes.add(key)
                lines.append(f"    └ {note[:150]}")
                shown += 1
                if shown >= 2:
                    break
            return False
        formats = info.get("formats") or []
        heights = [f.get("height") for f in formats if f.get("height")]
        title = (info.get("title") or "")[:45]
        ok_clients.append(name)
        lines.append(
            f"{name}: OK — {title} | форматов {len(formats)}, "
            f"макс {max(heights) if heights else '?'}p"
        )
        return True

    for client in clients:
        if try_client(client):
            break

    if not ok_clients:
        lines.append("")
        lines.append("углублённая проверка (ни один клиент не ответил):")
        lines.append(cookie_login_state())
        lines.append(_egress_report())
        proxy_egress = _proxy_egress_report()
        if proxy_egress:
            lines.append(proxy_egress)
        lines.append(_yt_page_probe(video_id))

        lines.append("")
        lines.append("повтор только по IPv4:")
        for client in ("web_embedded", "tv_samsung"):
            if try_client(client, {"force_ipv4": True}, " (IPv4)"):
                break

        if not ok_clients and COOKIES_FILE:
            lines.append("")
            lines.append("без cookies:")
            try_client("web_embedded", {"cookiefile": None}, " (без cookies)")

    lines.append("")
    lines.append("после проверки — " + _cookie_report())
    if ok_clients:
        lines.append("Итог: работают — " + ", ".join(ok_clients))
    else:
        lines.append("Итог: ни один клиент не ответил.")
        login_ok = any(line.startswith("вход по cookies: есть") for line in lines)
        bot_check = any(
            "sign in to confirm" in line.lower() or "not a bot" in line.lower()
            for line in lines
        )
        if "НЕТ" in js_report:
            lines.append(
                "На сервере нет JS-движка — с cookies YouTube почти всегда "
                "отказывает. В образе должен быть Deno (см. Dockerfile)."
            )
        elif bot_check and login_ok:
            if _yt_proxy():
                lines.append(
                    "Cookies рабочие, прокси включён, но YouTube всё равно требует "
                    "вход — выходной IP туннеля тоже помечен как бот. Нужен другой "
                    "выход WARP/прокси."
                )
            else:
                lines.append(
                    "Cookies рабочие, но YouTube требует вход — IP сервера помечен "
                    "как бот. Это блокировка датацентра: в образе помогает WARP "
                    "(WARP=1), вручную — YT_PROXY."
                )
        elif bot_check:
            lines.append(
                "YouTube требует вход, а cookies не входят в аккаунт — "
                "пересоберите cookies из браузера с живым входом."
            )
        elif login_ok:
            if _yt_proxy():
                lines.append(
                    "Прокси включён, но YouTube не отдаёт плеер. Проверьте, что "
                    "туннель живой (строка «IP через прокси» выше), и повторите."
                )
            else:
                lines.append(
                    "Вход по cookies работает, но YouTube не отдаёт плеер с этого IP. "
                    "Помогает WARP (WARP=1) или YT_PROXY."
                )
    return "\n".join(lines)


async def yt_diagnose(url: str) -> str:
    return await asyncio.to_thread(_yt_diagnose_sync, url)


def _download_media_sync(
    url: str, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()

    url = _public_url(url)

    # Reddit: свой путь через публичный Atom-фид (JSON и yt-dlp требуют вход).
    reddit_error: DownloadError | None = None
    if _is_reddit(url):
        try:
            return _reddit_download_sync(url, out_dir, "auto", on_progress, cancel)
        except _RedditExternal as external:
            url = external.url
            log.info("Reddit: в посте внешняя ссылка, отдаю yt-dlp: %s", url)
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            reddit_error = exc
            log.warning("Reddit не осилил %s: %s", url, exc)

    # cobalt: сервисы из COBALT_SERVICES (YouTube — только при COBALT_YOUTUBE=1).
    # Если он не справился — работает обычный путь yt-dlp. При обоюдной ошибке
    # показываем причину от cobalt, если yt-dlp упёрся в проверку «я не робот».
    cobalt_error: str | None = None
    if _use_cobalt(url):
        try:
            return _cobalt_download_sync(url, out_dir, "auto", on_progress, cancel)
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            cobalt_error = str(exc)
            log.warning("cobalt не осилил %s: %s", url, exc)
            _clear_dir(out_dir)

    # Pinterest: cobalt часто требует токен, поэтому есть свой разбор страницы.
    if _is_pinterest(url):
        return _pinterest_download_sync(url, out_dir, "auto", on_progress, cancel)

    # VK: vk.ru → vk.com (yt-dlp понимает посты на стене только на vk.com)
    if _is_vk(url):
        url = _vk_url(url)

    # Hitmo: yt-dlp не умеет его страницы треков, качаем своим путём
    if _is_hitmo(url):
        return _hitmo_from_url_sync(url, out_dir, on_progress, cancel)

    # Zvyki: тоже свой разбор, отдаёт AAC
    if _is_zvyki(url):
        return _zvyki_from_url_sync(url, out_dir, on_progress, cancel)

    # FurAffinity: yt-dlp его не знает, а страницы закрыты Cloudflare
    if _is_fa(url):
        return _fa_from_url_sync(url, out_dir, on_progress)

    # 1) лёгкий проход без скачивания: узнаём форматы, размеры, метаданные
    def _probe():
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        return _extract_with_rescue(
            url, _opts() | {"skip_download": True}, cancel=cancel
        )

    try:
        info = _probe()
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
    except DownloadCancelled:
        raise
    except Exception as exc:
        if reddit_error is not None:
            raise reddit_error from exc
        if cobalt_error and _yt_bot_check(exc):
            raise DownloadError(cobalt_error) from exc

        # X: yt-dlp понимает только видео, картинки берём через публичный API
        fallback = _x_media_sync(url, out_dir)
        if fallback:
            log.info("X: медиа получено через API (%s)", fallback[0].name)
            return fallback

        # VK: фото-страницы и посты yt-dlp не разбирает — берём вложение из
        # OG-тегов, которые VK отдаёт боту предпросмотра
        og = _vk_og_media(url) if _is_vk(url) else None
        if not og:
            raise
        kind, media = og
        if kind == "embed":
            log.info("VK: видео найдено на странице: %s", media)
            url = media
            info = _probe()
        else:
            return _vk_download_direct(kind, media, out_dir)

    if not info:
        fallback = _x_media_sync(url, out_dir)
        if fallback:
            return fallback
        if reddit_error is not None:
            raise reddit_error
        raise DownloadError("Не удалось разобрать ссылку — возможно, сервис не поддерживается.")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            # VK: пост без аудио — возможно, там фото или видео
            og = _vk_og_media(url) if _is_vk(url) else None
            if og and og[0] == "embed":
                log.info("VK: видео найдено на странице: %s", og[1])
                url = og[1]
                info = _probe()
                entries = (
                    [e for e in (info.get("entries") or []) if e]
                    if info.get("_type") == "playlist"
                    else [info]
                )
            elif og:
                return _vk_download_direct(og[0], og[1], out_dir)
            if not entries:
                raise DownloadError("По ссылке не нашлось медиа — в посте нет видео или фото.")
        entry = entries[0]

        # пост на стене: внутри лежит отдельное видео — качаем именно его
        entry_url = (entry.get("webpage_url") or entry.get("url") or "").strip()
        if entry_url.startswith("http") and entry_url != url:
            url = entry_url
            try:
                info = _probe()
            except Exception as exc:  # noqa: BLE001
                log.warning("VK: вложение не разобралось (%s): %s", entry_url, exc)
                info = entry
        else:
            info = entry

        # вложение-аудио: страницы нет, зато есть прямой URL файла
        if not info.get("formats") and entry.get("formats"):
            direct = (entry["formats"][-1] or {}).get("url")
            if direct:
                data = _http_get(direct, timeout=120)
                ext = ".m4a" if (entry["formats"][-1] or {}).get("ext") == "m4a" else ".mp3"
                path = out_dir / f"{entry.get('id') or 'vk'}{ext}"
                path.write_bytes(data)
                return path, entry

    # VK: страница без видеоформатов (фото-пост) — берём картинку из OG-тегов
    if _is_vk(url) and not info.get("formats"):
        og = _vk_og_media(url)
        if og and og[0] != "embed":
            return _vk_download_direct(og[0], og[1], out_dir)

    budget = MAX_UPLOAD_MB * 1024 * 1024
    last_error: Exception | None = None
    too_big = False

    # 2) пробуем форматы по очереди, пока файл не влезет в лимит Telegram
    for fmt in _format_candidates(info, budget):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        # подчищаем недокачанные файлы от предыдущей попытки
        for leftover in out_dir.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

        opts = _opts() | {
            "format": fmt,
            "merge_output_format": "mp4",
            "playlist_items": "1",
            "outtmpl": str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
            "progress_hooks": _progress_hook(on_progress, budget, cancel),
        }
        try:
            data = _extract_with_rescue(url, opts, download=True, cancel=cancel)
        except DownloadCancelled:
            raise
        except _TooBig:
            # скачивание прервано на лимите — пробуем качество пониже
            too_big = True
            log.info("format %s не влез в %s МБ, понижаю", fmt, MAX_UPLOAD_MB)
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("format %s failed for %s: %s", fmt, url, exc)
            continue

        files = [p for p in out_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not files:
            continue  # файл не влез в лимит — пробуем качество пониже

        path = max(files, key=lambda p: p.stat().st_size)
        return path, data

    if too_big:
        raise DownloadError(
            f"Файл больше лимита Telegram ({MAX_UPLOAD_MB} МБ) даже в низком качестве.\n"
            "Пришлите ссылку покороче или выберите «Только звук»."
        )

    if last_error is not None:
        if reddit_error is not None:
            raise reddit_error
        if cobalt_error and _yt_bot_check(last_error):
            raise DownloadError(cobalt_error)
        raise DownloadError(friendly_error(last_error, url))

    if reddit_error is not None:
        raise reddit_error

    raise DownloadError(
        f"Скачать не удалось: файл весит больше {MAX_UPLOAD_MB} МБ "
        "или сервис не отдал данные."
    )


async def download_media(
    url: str, out_dir: Path, on_progress=None, cancel=None
) -> tuple[Path, dict]:
    try:
        return await asyncio.to_thread(
            _download_media_sync, url, out_dir, on_progress, cancel
        )
    except (DownloadError, DownloadCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("media download failed: %s", url)
        raise DownloadError(friendly_error(exc, url)) from exc
