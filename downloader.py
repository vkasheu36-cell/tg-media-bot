"""Вся работа с yt-dlp: поиск треков, скачивание аудио и медиа по ссылке."""
from __future__ import annotations

import asyncio
import html
import http.cookiejar
import json
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp

from config import (
    COOKIES_FILE,
    FFMPEG_DIR,
    MAX_UPLOAD_MB,
    POT_SERVER,
    PROXY,
    YT_PLAYER_CLIENT,
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


@dataclass(slots=True)
class Track:
    provider: str  # "hm" (Hitmo), "yt" (YouTube) или "sc" (SoundCloud)
    video_id: str
    url: str
    title: str
    uploader: str
    duration: int


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
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    extractor_args: dict[str, dict[str, list[str]]] = {}
    if YT_PLAYER_CLIENT:
        extractor_args["youtube"] = {
            "player_client": [c.strip() for c in YT_PLAYER_CLIENT.split(",") if c.strip()]
        }
    if POT_SERVER:
        # локальный провайдер PO-токенов: YouTube считает запрос «браузерным»
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_SERVER]}
    if extractor_args:
        opts["extractor_args"] = extractor_args
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


_SERVICES = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "tt.site": "TikTok",
    "instagram.com": "Instagram",
    "twitter.com": "X",
    "x.com": "X",
    "vk.com": "VK",
    "vkvideo.ru": "VK",
    "reddit.com": "Reddit",
    "vimeo.com": "Vimeo",
    "soundcloud.com": "SoundCloud",
    "hitmoz.org": "Hitmo",
    "zvyki.com": "Zvyki",
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
        return f"{prefix}нет подходящего качества.\nПопробуйте другую ссылку."
    if "no video could be found" in low:
        return f"{prefix}в этом посте нет видео, а картинку получить не удалось."
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            f"{prefix}не пускает сервер без входа.\n"
            "Если это YouTube — он ботом не поддерживается. Для Instagram/TikTok\n"
            "помогают cookies аккаунта: COOKIES_FILE или YT_COOKIES."
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


def _fetch_text(url: str, user_agent: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
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


def _yandex_query_sync(url: str) -> str | None:
    """Публичный API Яндекс.Музыки работает без токена."""
    match = YANDEX_MUSIC_RE.search(url)
    album_match = YANDEX_ALBUM_RE.search(url)
    if match:
        api = f"https://api.music.yandex.net/tracks/{match.group(1)}"
    elif album_match:
        api = f"https://api.music.yandex.net/albums/{album_match.group(1)}"
    else:
        return None

    data = json.loads(_fetch_text(api, "Mozilla/5.0"))
    result = data.get("result")
    if isinstance(result, list):
        item = result[0] if result else None
    else:
        item = result
    if not isinstance(item, dict):
        return None
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or []).strip(", ")
    title = item.get("title") or ""
    return f"{artists} {title}".strip() or None


async def resolve_music_link(url: str) -> str | None:
    """Превращает ссылку стриминга в поисковый запрос «исполнитель название»."""
    if SPOTIFY_RE.search(url) or SPOTIFY_ALBUM_RE.search(url):
        func = _spotify_query_sync
    elif APPLE_MUSIC_RE.search(url):
        func = _apple_query_sync
    elif YANDEX_MUSIC_RE.search(url) or YANDEX_ALBUM_RE.search(url):
        func = _yandex_query_sync
    else:
        return None

    try:
        return await asyncio.wait_for(asyncio.to_thread(func, url), timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.warning("music link resolve failed: %s: %s", url, exc)
        return None


# ---------------------------------------------------------------- поиск музыки


def _search_sync(query: str, provider: str, limit: int) -> list[Track]:
    prefix = "scsearch"
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
) -> bytes:
    """Запрос к Hitmo с браузерными заголовками и повторами.

    WAF отвечает 403, если заголовки не похожи на браузерные, и любит
    обрывать SSL — поэтому пробуем несколько раз, обновляя cookie-сессию.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={**HITMO_HEADERS, "Referer": referer})
        try:
            with opener.open(request, timeout=timeout) as response:
                if on_progress is None:
                    return response.read()
                total = response.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else None
                chunks: list[bytes] = []
                done = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    done += len(chunk)
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
    track: Track, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    """Hitmo отдаёт готовый mp3, поэтому качаем напрямую, без yt-dlp."""
    opener = _hitmo_opener()
    try:
        _hitmo_get(HITMO_BASE + "/", opener, attempts=2)  # cookie сессии
    except Exception as exc:  # noqa: BLE001 — без cookie тоже может сработать
        log.debug("Hitmo: не удалось открыть главную: %s", exc)

    data = _hitmo_get(track.url, opener, referer=HITMO_BASE + "/", on_progress=on_progress)
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


def _hitmo_from_url_sync(url: str, out_dir: Path, on_progress=None) -> tuple[Path, dict]:
    """Ссылка на Hitmo из чата: страница трека или прямой mp3."""
    if _HITMO_MP3_RE.search(urlsplit(url).path):
        stem = urllib.parse.unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        title = re.sub(r"\.mp3$", "", stem, flags=re.I)
        title = re.sub(r"_(?:b[0-9a-f]{6,}|\d{5,})$", "", title)
        title = title.replace("_", " ").strip() or "Hitmo"
        track = Track(provider="hm", video_id=url, url=url, title=title, uploader="", duration=0)
    else:
        track = _hitmo_song_track_sync(url)
    return _hitmo_download_sync(track, out_dir, on_progress)


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


def _zvyki_fetch_file(url: str, dest: Path, on_progress=None) -> int:
    safe = urllib.parse.quote(url, safe=":/?&=%(),._-~+")
    request = urllib.request.Request(
        safe, headers={**ZVYKI_HEADERS, "Referer": ZVYKI_BASE + "/"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        total = response.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        with dest.open("wb") as fh:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
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
    track: Track, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if track.uploader:
        name = _safe_track_name(f"{track.uploader} - {track.title}")
    else:
        name = _safe_track_name(track.title)

    # Zvyki отдаёт то mp3, то голый AAC — определяем по первым байтам
    raw = out_dir / f"{name}.tmp"
    size = _zvyki_fetch_file(track.url, raw, on_progress)
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


def _zvyki_from_url_sync(url: str, out_dir: Path, on_progress=None) -> tuple[Path, dict]:
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
    return _zvyki_download_sync(track, out_dir, on_progress)


def is_youtube(url: str) -> bool:
    """YouTube убран из бота: с серверных IP он отдаёт «подтвердите вход»."""
    host = (urlsplit(url).netloc or "").lower()
    return (
        host.endswith("youtube.com")
        or host.endswith("youtu.be")
        or host.endswith("youtube-nocookie.com")
    )


YOUTUBE_DISABLED = (
    "YouTube больше не скачивается: с серверного IP он требует вход.\n"
    "Пришлите ссылку на другой сервис или название трека — найду в музыке."
)


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


async def search(query: str, limit: int = 12, budget: float | None = None) -> list[Track]:
    """Ищет треки: Hitmo и Zvyki (готовые файлы), затем SoundCloud.

    ``budget`` — сколько секунд ждать (для inline-режима). Если Hitmo уже дал
    достаточно результатов, медленные источники не трогаем вовсе.
    """
    deadline = time.monotonic() + budget if budget else None

    # «A - B» отправляем поисковикам без тире: они лучше понимают набор слов,
    # а к какому полю относится каждая часть, разберёмся при ранжировании.
    parts = _split_query(query)
    query_for_search = " ".join(parts) if parts else query

    hm_timeout = 12.0 if deadline is None else max(1.0, min(12.0, deadline - time.monotonic()))
    try:
        tracks = await asyncio.wait_for(
            _search_one(query_for_search, "hm", min(limit, 30)), timeout=hm_timeout
        )
    except Exception:  # noqa: BLE001 — таймаут или сбой Hitmo
        tracks = []

    # Hitmo редко отдаёт больше десятка треков — добираем Zvyki и SoundCloud,
    # иначе список обрывается на нескольких результатах и страниц почти нет.
    if len(tracks) < limit:
        needed = max(4, limit - len(tracks))
        tasks = [
            asyncio.create_task(_search_one(query_for_search, provider, needed))
            for provider in ("zv", "sc")
        ]
        if deadline is not None:
            wait_for_extra = max(0.0, deadline - time.monotonic())
        elif tracks:
            # Hitmo уже что-то нашёл — не заставляем человека ждать медленные источники
            wait_for_extra = 3.0
        else:
            wait_for_extra = 20.0

        if wait_for_extra > 0:
            await asyncio.wait(set(tasks), timeout=wait_for_extra)

        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is None:
                tracks.extend(task.result())
            elif not task.done():
                task.add_done_callback(lambda t: t.cancelled() or t.exception())

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


def _progress_hook(on_progress):
    """Мост между потоком yt-dlp и прогресс-баром в сообщении."""
    if on_progress is None:
        return []

    def hook(data: dict) -> None:
        status = data.get("status")
        if status == "downloading":
            done = int(data.get("downloaded_bytes") or 0)
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            on_progress(done, int(total) if total else None)
        elif status == "finished":
            done = int(data.get("downloaded_bytes") or data.get("total_bytes") or 0)
            total = int(data.get("total_bytes") or done) or None
            on_progress(done, total)

    return [hook]


def _download_audio_sync(
    track: Track, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)

    if track.provider == "yt" or is_youtube(track.url):
        raise DownloadError(YOUTUBE_DISABLED)

    if track.provider == "hm":
        return _hitmo_download_sync(track, out_dir, on_progress)
    if track.provider == "zv":
        return _zvyki_download_sync(track, out_dir, on_progress)
    if _is_hitmo(track.url):
        # ссылка на страницу трека из Hitmo: разбираем её и качаем mp3
        return _hitmo_from_url_sync(track.url, out_dir, on_progress)
    if _is_zvyki(track.url):
        return _zvyki_from_url_sync(track.url, out_dir, on_progress)

    opts = _opts() | {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title).80s.%(ext)s"),
        "writethumbnail": True,
        "progress_hooks": _progress_hook(on_progress),
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

    def _fetch():
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(_public_url(track.url), download=True)
            return ydl.sanitize_info(data) if data else {}

    info = _dns_retry(_fetch)

    mp3_files = sorted(out_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if mp3_files:
        return mp3_files[0], info

    # ffmpeg мог отсутствовать — отдадим что есть, лишь бы это было аудио
    fallback = [p for p in out_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS]
    if fallback:
        return fallback[0], info

    raise DownloadError("Не удалось извлечь аудио (проверьте, установлен ли ffmpeg).")


async def download_audio(
    track: Track, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    try:
        return await asyncio.to_thread(_download_audio_sync, track, out_dir, on_progress)
    except DownloadError:
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

    Важно: фильтр ``height<=N`` отбрасывает форматы без метаданных о высоте
    (частый случай у TikTok), поэтому первым всегда идёт вариант без фильтра.
    """
    candidates = ["bv*+ba/b"]

    checks = [(cap, _size_fits(info, cap, budget)) for cap in (1080, 720, 480, 360)]

    if all(fits is None for _, fits in checks):
        # размеры вообще неизвестны (TikTok и подобные) — не перебираем кап
        return ["bv*+ba/b", "b"]

    if checks[0][1]:  # 1080p влезает целиком
        return ["bv*+ba/b", "b"]

    for cap, fits in checks:
        if fits is False:
            continue  # не влезает
        candidates.append(f"bv*[height<={cap}]+ba/b[height<={cap}]/b")
        if fits:  # нашли подходящий потолок — ниже смысла нет
            break

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


def _download_media_sync(
    url: str, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)

    if is_youtube(url):
        raise DownloadError(YOUTUBE_DISABLED)

    url = _public_url(url)

    # Hitmo: yt-dlp не умеет его страницы треков, качаем своим путём
    if _is_hitmo(url):
        return _hitmo_from_url_sync(url, out_dir, on_progress)

    # Zvyki: тоже свой разбор, отдаёт AAC
    if _is_zvyki(url):
        return _zvyki_from_url_sync(url, out_dir, on_progress)

    # 1) лёгкий проход без скачивания: узнаём форматы, размеры, метаданные
    def _probe():
        with yt_dlp.YoutubeDL(_opts() | {"skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = _dns_retry(_probe)
    except Exception:
        # X: yt-dlp понимает только видео, картинки берём через публичный API
        fallback = _x_media_sync(url, out_dir)
        if fallback:
            log.info("X: медиа получено через API (%s)", fallback[0].name)
            return fallback
        raise

    if not info:
        fallback = _x_media_sync(url, out_dir)
        if fallback:
            return fallback
        raise DownloadError("Не удалось разобрать ссылку — возможно, сервис не поддерживается.")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("По ссылке не нашлось медиа.")
        info = entries[0]

    budget = MAX_UPLOAD_MB * 1024 * 1024
    last_error: Exception | None = None

    # 2) пробуем форматы по очереди, пока файл не влезет в лимит Telegram
    for fmt in _format_candidates(info, budget):
        # подчищаем недокачанные файлы от предыдущей попытки
        for leftover in out_dir.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

        opts = _opts() | {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
            "max_filesize": budget,
            "progress_hooks": _progress_hook(on_progress),
        }
        try:
            def _fetch():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    data = ydl.extract_info(url, download=True)
                    return ydl.sanitize_info(data) if data else {}

            data = _dns_retry(_fetch)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("format %s failed for %s: %s", fmt, url, exc)
            continue

        files = [p for p in out_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not files:
            continue  # файл не влез в max_filesize — пробуем качество пониже

        path = max(files, key=lambda p: p.stat().st_size)
        return path, data

    if last_error is not None:
        raise DownloadError(friendly_error(last_error, url))

    raise DownloadError(
        f"Скачать не удалось: файл весит больше {MAX_UPLOAD_MB} МБ "
        "или сервис не отдал данные."
    )


async def download_media(
    url: str, out_dir: Path, on_progress=None
) -> tuple[Path, dict]:
    try:
        return await asyncio.to_thread(_download_media_sync, url, out_dir, on_progress)
    except DownloadError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("media download failed: %s", url)
        raise DownloadError(friendly_error(exc, url)) from exc
