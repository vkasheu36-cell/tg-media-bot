"""Обработчики сообщений: поиск музыки и скачивание медиа по ссылке."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import storage
from config import DOWNLOADS_DIR, MAX_UPLOAD_MB
from downloader import (
    AUDIO_EXTS,
    SEMAPHORE as _semaphore,
    DownloadError,
    Track,
    YOUTUBE_DISABLED,
    download_audio,
    download_media,
    is_music_link,
    is_youtube,
    resolve_music_link,
    search,
)

log = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8
SESSION_TTL = 30 * 60  # полчаса — потом меню считается устаревшим

URL_RE = re.compile(r"https?://[^\s<>\"']+")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".gif"}

START_TEXT = (
    "👋 <b>Привет! Я умею две вещи:</b>\n\n"
    "🎵 <b>Музыка</b> — пришли название трека или «исполнитель - название», "
    "я найду варианты и пришлю mp3.\n\n"
    "📥 <b>Медиа по ссылке</b> — кинь ссылку на YouTube (включая Shorts), TikTok, "
    "Twitter/X, Instagram, Vimeo, Reddit, SoundCloud и многое другое — пришлю файл.\n\n"
    "💬 <b>В любом чате</b> — набери <code>@имя_бота</code> и ссылку или название трека, "
    "не добавляя меня в чат.\n\n"
    "Лимит Telegram — 50 МБ на файл, поэтому видео автоматически ужимается по качеству."
)

HELP_TEXT = (
    "<b>Как пользоваться</b>\n\n"
    "• <code>девочка с дайвинчика</code> — поиск музыки\n"
    "• <code>https://www.tiktok.com/...</code> — скачать видео\n"
    "• <code>@имя_бота https://tiktok.com/...</code> — прямо в любом чате\n\n"
    "Если YouTube отвечает «подтвердите, что вы не робот» — нужны cookies (README).\n"
    "Ссылки на посты Telegram не поддерживаются: для них нужен юзербот."
)


@dataclass
class SearchSession:
    user_id: int
    query: str
    tracks: list[Track]
    created_at: float


_sessions: dict[str, SearchSession] = {}


# ---------------------------------------------------------------- утилиты


def fmt_duration(seconds: int) -> str:
    if not seconds:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def safe_filename(name: str, ext: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{cleaned[:120]}{ext}"


def _gc_sessions() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v.created_at > SESSION_TTL]:
        _sessions.pop(sid, None)


def _new_session(user_id: int, query: str, tracks: list[Track]) -> str:
    _gc_sessions()
    sid = uuid.uuid4().hex[:10]
    _sessions[sid] = SearchSession(user_id=user_id, query=query, tracks=tracks, created_at=time.time())
    return sid


def _track_label(track: Track, limit: int = 60) -> str:
    """«2:36 СТИНТ — Телепорт»: исполнитель виден сразу, название режется первым."""
    duration = fmt_duration(track.duration)
    title = " ".join(track.title.split())
    artist = " ".join((track.uploader or "").split())
    dur = f"{duration} " if duration else ""

    # у SoundCloud и Hitmo исполнитель часто уже внутри названия
    if not artist or artist.lower() in title.lower():
        text = f"{dur}{title}"
        if len(text) <= limit:
            return text
        return f"{dur}{title[: limit - len(dur) - 1]}…"

    head = f"{dur}{artist} — "
    if len(head) + len(title) <= limit:
        return head + title

    room = limit - len(head) - 1
    if room >= 12:
        return f"{head}{title[:room]}…"
    # очень длинный исполнитель — режем всю строку
    return f"{head}{title}"[: limit - 1] + "…"


def results_keyboard(sid: str, page: int, tracks: list[Track]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for offset, track in enumerate(tracks[start : start + PAGE_SIZE]):
        idx = start + offset
        kb.row(
            InlineKeyboardButton(
                text=_track_label(track),
                callback_data=f"tr:{sid}:{idx}",
            )
        )

    total_pages = max(1, (len(tracks) + PAGE_SIZE - 1) // PAGE_SIZE)
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="«", callback_data=f"pg:{sid}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="»", callback_data=f"pg:{sid}:{page + 1}"))
        kb.row(*nav)

    return kb.as_markup()


def _get_session(cb: CallbackQuery, sid: str) -> SearchSession | None:
    session = _sessions.get(sid)
    if not session or session.user_id != cb.from_user.id:
        return None
    return session


async def _cleanup_notice(target: Message, text: str) -> None:
    try:
        await target.edit_text(text)
    except Exception:  # noqa: BLE001 — сообщение могли удалить
        pass


class ProgressBar:
    """Прогресс скачивания в сообщении «Скачиваю…».

    Обновляем текст не чаще раза в 2 секунды, чтобы не упереться в лимиты
    Telegram. ``update()`` вызывается из потока загрузки и только меняет числа.
    """

    BAR_LEN = 10

    def __init__(self, status: Message, header: str, interval: float = 2.0) -> None:
        self.status = status
        self.header = header
        self.interval = interval
        self.done = 0
        self.total: int | None = None
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    def update(self, done: int, total: int | None) -> None:
        self.done = done
        # куски HLS/DASH отдают «свой» маленький total — берём максимальный,
        # иначе полоска прыгает с 6% на 100% и обратно
        if total and (self.total is None or total > self.total):
            self.total = total

    def _text(self) -> str:
        done_mb = f"{self.done / 1048576:.1f}".replace(".", ",")
        if not self.total:
            return f"{self.header}\n{done_mb} МБ"

        pct = min(99, int(self.done * 100 / self.total))
        filled = int(pct / 100 * self.BAR_LEN)
        bar = "▰" * filled + "▱" * (self.BAR_LEN - filled)
        total_mb = f"{self.total / 1048576:.1f}".replace(".", ",")
        return f"{self.header}\n{bar} {pct}%\n{done_mb} из {total_mb} МБ"

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.interval)
            if self._stopped or not self.done:
                continue
            try:
                await self.status.edit_text(self._text())
            except Exception:  # noqa: BLE001 — «message is not modified» и подобное
                pass

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def _remember(message: Message, key: str, title: str) -> None:
    """Запоминает file_id, чтобы потом отдавать этот файл в inline-режиме."""
    kind = file_id = thumb = None
    if message.video:
        kind, file_id = "video", message.video.file_id
        thumb = message.video.thumbnail.file_id if message.video.thumbnail else None
    elif message.audio:
        kind, file_id = "audio", message.audio.file_id
        thumb = message.audio.thumbnail.file_id if message.audio.thumbnail else None
    elif message.document:
        kind, file_id = "document", message.document.file_id
    elif message.photo:
        kind, file_id = "photo", message.photo[-1].file_id

    if kind and file_id:
        storage.save_media(key, kind, file_id, title, thumb)


# ---------------------------------------------------------------- доставка файла


def _build_media(path: Path, caption: str, title: str, performer: str, duration: int | None):
    """InputMedia для edit_media — им же статус превращается в готовый файл."""
    ext = path.suffix.lower()
    file = FSInputFile(path, filename=safe_filename(title, ext))
    if ext in AUDIO_EXTS:
        return InputMediaAudio(
            media=file, caption=caption, title=title, performer=performer or None, duration=duration
        ), file
    if ext in IMAGE_EXTS:
        return InputMediaPhoto(media=file, caption=caption), file
    if ext in VIDEO_EXTS:
        return InputMediaVideo(
            media=file, caption=caption, duration=duration, supports_streaming=True
        ), file
    return InputMediaDocument(media=file, caption=caption), file


async def _send_new(
    status: Message, path: Path, caption: str, title: str, performer: str, duration: int | None
) -> Message:
    ext = path.suffix.lower()
    file = FSInputFile(path, filename=safe_filename(title, ext))
    if ext in AUDIO_EXTS:
        return await status.answer_audio(
            audio=file, caption=caption, title=title, performer=performer or None, duration=duration
        )
    if ext in IMAGE_EXTS:
        return await status.answer_photo(photo=file, caption=caption)
    if ext in VIDEO_EXTS:
        return await status.answer_video(
            video=file, caption=caption, duration=duration, supports_streaming=True
        )
    return await status.answer_document(document=file, caption=caption)


def _track_caption(title: str, performer: str) -> str:
    """Подпись у музыки — только исполнитель и название, без ссылки на источник."""
    parts = [html.escape(p.strip()) for p in (performer, title) if p and p.strip()]
    return " — ".join(parts)


def _media_caption(path: Path, title: str, performer: str, source_url: str) -> str:
    if path.suffix.lower() in AUDIO_EXTS:
        return _track_caption(title, performer)
    return f'<a href="{html.escape(source_url)}">ссылка</a>'


async def deliver(
    status: Message,
    path: Path,
    info: dict,
    source_url: str,
    cache_key: str,
    fallback_title: str = "",
    fallback_performer: str = "",
) -> Message | None:
    """Как у cobold: сообщение «скачиваю» превращается в готовое медиа.

    Если Telegram не даёт отредактировать текстовое сообщение в медиа —
    отправляем файл отдельным сообщением, а статус удаляем.
    """
    ext = path.suffix.lower()
    title = (info.get("track") or info.get("title") or fallback_title or path.stem)[:180]
    performer = (info.get("artist") or info.get("uploader") or fallback_performer or "")[:120]
    duration = int(info.get("duration") or 0) or None
    caption = _media_caption(path, title, performer, source_url)
    cache_title = f"{performer} - {title}" if performer else title

    media, _ = _build_media(path, caption, title, performer, duration)
    try:
        sent = await status.edit_media(media)
        _remember(sent, cache_key, cache_title)
        return sent
    except Exception as exc:  # noqa: BLE001
        log.warning("edit_media failed (%s), falling back to new message", exc)

    sent = await _send_new(status, path, caption, title, performer, duration)
    _remember(sent, cache_key, cache_title)
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    return sent


# ---------------------------------------------------------------- команды


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(message: Message, command: CommandObject) -> None:
    payload = (command.args or "").strip()
    if payload.startswith("dl_"):
        item = storage.pop_pending(payload[3:])
        if not item:
            await message.answer("Ссылка устарела — отправь её ещё раз.")
            return
        await handle_link(message, item["url"])
    elif payload.startswith("q_"):
        item = storage.pop_pending(payload[2:])
        if not item:
            await message.answer("Запрос устарел — напиши название трека ещё раз.")
            return
        await handle_music(message, item["query"])
    else:
        await cmd_start(message)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=_start_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


def _start_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎵 Найти музыку", callback_data="hint:music"))
    kb.row(InlineKeyboardButton(text="📥 Скачать по ссылке", callback_data="hint:link"))
    return kb.as_markup()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("hint:"))
async def cb_hint(callback: CallbackQuery) -> None:
    if callback.data == "hint:music":
        await callback.answer("Просто напиши название трека 🙂", show_alert=True)
    else:
        await callback.answer("Кинь ссылку на видео — TikTok, YouTube, X и т.д.", show_alert=True)


# ---------------------------------------------------------------- текст


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/") or message.from_user is None:
        return

    # сообщение, отправленное через inline-бота (наш же плейсхолдер «Скачиваю…»),
    # нельзя принимать за поисковый запрос
    if message.via_bot is not None:
        return

    match = URL_RE.search(text)
    if match:
        url = match.group(0).rstrip(".,;:!?)\"'»]")
        if is_music_link(url):
            await handle_music_link(message, url)
        else:
            await handle_link(message, url)
        return

    await handle_music(message, text)


async def handle_music_link(message: Message, url: str) -> None:
    """Spotify/Apple Music/Яндекс.Музыка → ищем трек по «исполнитель название»."""
    status = await message.answer("🔗 Разбираю ссылку…")
    query = await resolve_music_link(url)
    if not query:
        await _cleanup_notice(
            status,
            "❌ Не удалось разобрать ссылку.\n"
            "Пришлите ссылку на конкретный трек или название текстом.",
        )
        return
    await handle_music(message, query, status=status)


# ---------------------------------------------------------------- музыка


async def handle_music(message: Message, query: str, status: Message | None = None) -> None:
    if len(query) < 2:
        await message.answer("Слишком короткий запрос — напишите хотя бы пару слов.")
        return
    query = query[:300]

    if status is None:
        status = await message.answer(f"🔎 Ищу «{html.escape(query)}»…")
    else:
        try:
            await status.edit_text(f"🔎 Ищу «{html.escape(query)}»…")
        except Exception:  # noqa: BLE001
            pass

    try:
        tracks = await search(query)
    except Exception:  # noqa: BLE001
        log.exception("search failed: %s", query)
        await _cleanup_notice(status, "❌ Поиск сломался. Попробуйте ещё раз.")
        return

    if not tracks:
        await _cleanup_notice(status, "Ничего не нашлось. Попробуйте другой запрос.")
        return

    sid = _new_session(message.from_user.id, query, tracks)
    text = (
        f"🎵 <b>Аудиозаписи по запросу «{html.escape(query)}»</b>\n"
        "<i>Нажмите на трек — пришлю mp3.</i>"
    )
    try:
        await status.edit_text(text, reply_markup=results_keyboard(sid, 0, tracks))
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=results_keyboard(sid, 0, tracks))


async def download_track(message: Message, track: Track) -> None:
    status = await message.answer(f"⬇️ Качаю «{html.escape(track.title)}»…")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=DOWNLOADS_DIR))
    progress = ProgressBar(status, f"⬇️ Качаю «{html.escape(track.title)}»")
    try:
        try:
            async with _semaphore:
                path, info = await asyncio.wait_for(
                    download_audio(track, tmp_dir, on_progress=progress.update), timeout=240
                )
        except DownloadError as exc:
            await _cleanup_notice(status, f"❌ {html.escape(str(exc))}")
            return
        except asyncio.TimeoutError:
            await _cleanup_notice(status, "❌ Слишком долго. Попробуйте другой трек.")
            return
        except Exception:  # noqa: BLE001
            log.exception("track download failed: %s", track.url)
            await _cleanup_notice(status, "❌ Не получилось скачать трек.")
            return
        finally:
            await progress.stop()

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            raise DownloadError(f"Файл {size_mb:.0f} МБ — больше лимита Telegram.")

        await deliver(
            status,
            path,
            info,
            track.url,
            storage.normalize_url(track.url),
            fallback_title=track.title,
            fallback_performer=track.uploader,
        )
    except DownloadError as exc:
        await _cleanup_notice(status, f"❌ {html.escape(str(exc))}")
    except Exception:  # noqa: BLE001
        log.exception("track send failed: %s", track.url)
        await _cleanup_notice(status, "❌ Не получилось отправить трек.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.callback_query(F.data.startswith("pg:"))
async def cb_page(callback: CallbackQuery) -> None:
    _, sid, raw_page = callback.data.split(":", 2)
    session = _get_session(callback, sid)
    if not session:
        await callback.answer("Это меню устарело — отправьте запрос заново.", show_alert=True)
        return

    page = int(raw_page)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=results_keyboard(sid, page, session.tracks)
        )
    except Exception:  # noqa: BLE001 — «message is not modified» и подобное
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("tr:"))
async def cb_track(callback: CallbackQuery) -> None:
    _, sid, raw_idx = callback.data.split(":", 2)
    session = _get_session(callback, sid)
    if not session:
        await callback.answer("Это меню устарело — отправьте запрос заново.", show_alert=True)
        return

    idx = int(raw_idx)
    if idx >= len(session.tracks):
        await callback.answer("Трек не найден.", show_alert=True)
        return

    await callback.answer("Качаю…")
    await download_track(callback.message, session.tracks[idx])


# ---------------------------------------------------------------- медиа по ссылке


async def handle_link(message: Message, url: str) -> None:
    if is_youtube(url):
        await message.answer(YOUTUBE_DISABLED)
        return

    if any(host in url for host in ("t.me/", "telegram.me/", "telegram.dog/")):
        await message.answer(
            "Ссылки на посты Telegram я не открываю: для этого нужен юзербот "
            "с доступом к аккаунту, а это отдельная история.\n"
            "Пришлите прямую ссылку на видео (TikTok, YouTube, X и т.д.)."
        )
        return

    status = await message.answer("⬇️ Скачиваю…")
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=DOWNLOADS_DIR))
    progress = ProgressBar(status, "⬇️ Скачиваю…")

    try:
        try:
            async with _semaphore:
                path, info = await asyncio.wait_for(
                    download_media(url, tmp_dir, on_progress=progress.update), timeout=600
                )
        except DownloadError as exc:
            await _cleanup_notice(status, f"❌ {html.escape(str(exc))}")
            return
        except asyncio.TimeoutError:
            await _cleanup_notice(status, "❌ Скачивание зависло. Попробуйте ещё раз.")
            return
        except Exception:  # noqa: BLE001
            log.exception("media download failed: %s", url)
            await _cleanup_notice(status, "❌ Не получилось скачать. Проверьте ссылку.")
            return
        finally:
            await progress.stop()

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            raise DownloadError(
                f"Файл {size_mb:.0f} МБ — больше лимита Telegram ({MAX_UPLOAD_MB} МБ)."
            )

        await deliver(
            status,
            path,
            info,
            url,
            storage.normalize_url(url),
        )
    except DownloadError as exc:
        await _cleanup_notice(status, f"❌ {html.escape(str(exc))}")
    except Exception:  # noqa: BLE001
        log.exception("media send failed: %s", url)
        await _cleanup_notice(status, "❌ Не получилось отправить файл.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
