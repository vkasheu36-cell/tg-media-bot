"""Inline-режим: @бот <ссылка|название> прямо в любом чате, без добавления бота.

Работает как @coboldbot: бот мгновенно отвечает списком вариантов, а после
выбора пользователем **редактирует своё же inline-сообщение** — сначала там
висит «скачиваю…», потом сообщение превращается в видео/аудио или в ошибку.

Для этого нужны два условия:
  1. в @BotFather включён inline-режим: /setinline
  2. включены уведомления о выборе результата: /setinlinefeedback → 100%
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
)

import storage
from config import DOWNLOADS_DIR, MAX_UPLOAD_MB
from downloader import (
    AUDIO_EXTS,
    SEMAPHORE,
    DownloadError,
    Track,
    download_audio,
    download_media,
    is_music_link,
    resolve_music_link,
    search,
)
from handlers import IMAGE_EXTS, safe_filename

log = logging.getLogger(__name__)
router = Router()

SEARCH_TTL = 10 * 60
SEARCH_WAIT = 5.0  # столько inline-ответ ждёт поиск, дальше отвечаем тем, что есть
_search_cache: dict[str, tuple[float, list[Track]]] = {}
_search_tasks: dict[str, asyncio.Task] = {}
_bg_tasks: set[asyncio.Task] = set()


def _rid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt(seconds: int) -> str:
    if not seconds:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}:{sec:02d}"


def _dl_kb(token: str) -> InlineKeyboardMarkup:
    """Кнопка-подстраховка: работает, даже если chosen_inline_result не приходит."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬇️ Скачать", callback_data=f"inl:{token}")]]
    )


NO_KB = InlineKeyboardMarkup(inline_keyboard=[])


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _cached(key: str) -> list[Track] | None:
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_TTL:
        return hit[1]
    return None


def _closest_cached(key: str) -> list[Track]:
    """Пока точный поиск не готов, показываем результаты самого длинного
    закэшированного начала строки — пользователь набирал его секунду назад."""
    best_key = ""
    best: list[Track] = []
    for cached_key, (_, tracks) in _search_cache.items():
        if tracks and key.startswith(cached_key) and len(cached_key) > len(best_key):
            best_key, best = cached_key, tracks
    return best


async def _run_search(key: str, query: str, limit: int) -> list[Track]:
    try:
        tracks = await search(query, limit=limit, budget=max(1.0, SEARCH_WAIT - 0.5))
    except Exception:  # noqa: BLE001 — inline-поиск не должен падать наружу
        log.warning("inline search failed: %s", query, exc_info=True)
        return []
    if tracks:
        _search_cache[key] = (time.time(), tracks)
        if len(_search_cache) > 200:
            _search_cache.clear()
    return tracks


def _start_search(key: str, query: str, limit: int) -> asyncio.Task:
    """Один поиск на строку: одинаковые запросы не дублируются."""
    task = _search_tasks.get(key)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_run_search(key, query, limit))
    _search_tasks[key] = task
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def _search_fast(query: str, limit: int = 10) -> list[Track]:
    """Поиск, который укладывается в лимит inline-ответа (~10 с).

    Если не успели — отдаём то, что уже нашли для более короткого запроса,
    а сам поиск продолжается в фоне и пригодится на следующем нажатии клавиши.
    """
    key = _norm(query)
    cached = _cached(key)
    if cached is not None:
        return cached

    task = _start_search(key, query, limit)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=SEARCH_WAIT)
    except asyncio.TimeoutError:
        if len(_search_tasks) > 300:
            for k, t in list(_search_tasks.items()):
                if t.done():
                    _search_tasks.pop(k, None)
        return _closest_cached(key)


# ---------------------------------------------------------------- inline query


@router.inline_query()
async def on_inline(query: InlineQuery) -> None:
    text = (query.query or "").strip()
    try:
        me = await query.bot.me()
        username = me.username or ""

        if text.lower().startswith(("http://", "https://")):
            if is_music_link(text):
                resolved = await resolve_music_link(text)
                if resolved:
                    await _answer_music(query, resolved, username)
                    return
            await _answer_url(query, text, username)
        elif len(text) >= 2:
            await _answer_music(query, text, username)
        else:
            await query.answer([], cache_time=0, is_personal=True)
    except TelegramBadRequest as exc:
        # «query is too old» — пользователь уже набрал другое, ругаться не нужно
        log.info("inline query skipped: %s", exc.message)


async def _answer_url(query: InlineQuery, url: str, username: str) -> None:
    results: list = []

    for mode, title, description in (
        ("video", "📷 Фото-видео", "Скачать видео или картинку, до 50 МБ"),
        ("audio", "🎵 Только звук", "Извлечь дорожку в mp3"),
    ):
        token = storage.save_pending({"kind": "media", "url": url, "mode": mode})
        results.append(
            InlineQueryResultArticle(
                id=token,
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(message_text="⬇️ Скачиваю…"),
                reply_markup=_dl_kb(token),
            )
        )

    token = storage.save_pending({"kind": "query", "query": url})
    button = InlineQueryResultsButton(
        text="⬇️ Скачать в боте",
        start_parameter=f"dl_{token}",
    )
    await query.answer(results, button=button, cache_time=0, is_personal=True)


async def _answer_music(query: InlineQuery, text: str, username: str) -> None:
    results: list = []

    for track in await _search_fast(text):
        token = storage.save_pending(
            {
                "kind": "track",
                "track": {
                    "provider": track.provider,
                    "video_id": track.video_id,
                    "url": track.url,
                    "title": track.title,
                    "uploader": track.uploader,
                    "duration": track.duration,
                },
            },
            token=uuid.uuid4().hex[:16],
        )
        results.append(
            InlineQueryResultArticle(
                id=token,
                title=_short(f"{_fmt(track.duration)} {track.title}".strip(), 60),
                description=_short(track.uploader or "Нажмите, чтобы скачать mp3", 80),
                input_message_content=InputTextMessageContent(message_text="⬇️ Скачиваю…"),
                reply_markup=_dl_kb(token),
            )
        )

    token = storage.save_pending({"kind": "query", "query": text})
    button = InlineQueryResultsButton(
        text="🔎 Показать в боте",
        start_parameter=f"q_{token}",
    )
    await query.answer(results, button=button, cache_time=0, is_personal=True)


# ---------------------------------------------------------------- выбран результат


@router.chosen_inline_result()
async def on_chosen(result: ChosenInlineResult, bot: Bot) -> None:
    log.info(
        "chosen_inline_result: result_id=%s from=%s inline_message_id=%s",
        result.result_id,
        result.from_user.id if result.from_user else "?",
        result.inline_message_id,
    )
    if not result.inline_message_id:
        log.warning("нет inline_message_id — отредактировать сообщение не получится")
        return

    payload = storage.pop_pending(result.result_id)
    if not payload:
        log.warning("не найден отложенный запрос для result_id=%s", result.result_id)
        return
    log.info("отложенный запрос найден: %s", payload.get("kind"))

    if payload["kind"] == "media":
        coro = _inline_media(
            bot,
            result.inline_message_id,
            payload["url"],
            payload.get("mode", "video"),
            result.from_user.id if result.from_user else None,
        )
    elif payload["kind"] == "track":
        coro = _inline_track(
            bot,
            result.inline_message_id,
            Track(**payload["track"]),
            result.from_user.id if result.from_user else None,
        )
    else:
        return

    _spawn(coro)


def _spawn(coro) -> None:
    """Скачивание длится минутами — не держим обработчик обновления."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------- кнопка-подстраховка


@router.callback_query(F.data.startswith("inl:"))
async def on_download_button(callback: CallbackQuery, bot: Bot) -> None:
    """Страховка на случай, если Telegram не присылает chosen_inline_result.

    Кнопка «⬇️ Скачать» висит на самом сообщении «Скачиваю…»; нажатие
    обрабатывается как обычный callback и работает всегда.
    """
    if not callback.inline_message_id:
        await callback.answer("Кнопка работает только в сообщении из inline-режима.", show_alert=True)
        return

    token = (callback.data or "")[4:]
    payload = storage.pop_pending(token)
    if not payload:
        await callback.answer("Уже скачиваю этот файл.", show_alert=False)
        return

    await callback.answer("Скачиваю…")

    if payload["kind"] == "media":
        coro = _inline_media(
            bot,
            callback.inline_message_id,
            payload["url"],
            payload.get("mode", "video"),
            callback.from_user.id,
        )
    elif payload["kind"] == "track":
        coro = _inline_track(
            bot,
            callback.inline_message_id,
            Track(**payload["track"]),
            callback.from_user.id,
        )
    else:
        return

    _spawn(coro)


async def _inline_media(
    bot: Bot, inline_message_id: str, url: str, mode: str, user_id: int | None = None
) -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=DOWNLOADS_DIR))

    try:
        async with SEMAPHORE:
            if mode == "audio":
                stub = Track("url", "", url, "", "", 0)
                path, info = await asyncio.wait_for(download_audio(stub, tmp_dir), timeout=600)
            else:
                path, info = await asyncio.wait_for(download_media(url, tmp_dir), timeout=600)

        _check_size(path)
        media = _build_inline_media(path, info, url, mode)
        await _edit_inline(bot, inline_message_id, media, path, info, url, mode, user_id)
    except DownloadError as exc:
        await _inline_error(bot, inline_message_id, str(exc))
    except asyncio.TimeoutError:
        await _inline_error(bot, inline_message_id, "Слишком долго. Попробуйте ещё раз.")
    except Exception:  # noqa: BLE001
        log.exception("inline media failed: %s", url)
        await _inline_error(bot, inline_message_id, "Не получилось скачать. Проверьте ссылку.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _inline_track(
    bot: Bot, inline_message_id: str, track: Track, user_id: int | None = None
) -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=DOWNLOADS_DIR))

    try:
        async with SEMAPHORE:
            path, info = await asyncio.wait_for(download_audio(track, tmp_dir), timeout=240)

        _check_size(path)
        title = (info.get("track") or track.title or path.stem)[:180]
        performer = (info.get("artist") or track.uploader or "")[:120]
        media = InputMediaAudio(
            media=FSInputFile(path, filename=safe_filename(f"{performer} - {title}", path.suffix)),
            title=title,
            performer=performer or None,
            duration=int(info.get("duration") or track.duration or 0) or None,
            caption=f'<a href="{html.escape(track.url)}">ссылка</a>',
        )
        await _edit_inline(
            bot, inline_message_id, media, path, info, track.url, "audio", user_id
        )
    except DownloadError as exc:
        await _inline_error(bot, inline_message_id, str(exc))
    except asyncio.TimeoutError:
        await _inline_error(bot, inline_message_id, "Слишком долго. Попробуйте другой трек.")
    except Exception:  # noqa: BLE001
        log.exception("inline track failed: %s", track.url)
        await _inline_error(bot, inline_message_id, "Не получилось скачать трек.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _check_size(path: Path) -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise DownloadError(
            f"Файл {size_mb:.0f} МБ — больше лимита Telegram ({MAX_UPLOAD_MB} МБ)."
        )


async def _edit_inline(
    bot: Bot,
    inline_message_id: str,
    media,
    path: Path,
    info: dict,
    source_url: str,
    mode: str,
    user_id: int | None = None,
) -> None:
    """Заменяет «Скачиваю…» на файл, перебирая способы.

    Telegram то и дело отвечает «invalid message content specified» на
    редактирование именно inline-сообщения (в личке тот же файл проходит),
    поэтому пробуем несколько вариантов, пока один не сработает.
    """
    caption = getattr(media, "caption", None) or ""

    variants = [("загрузка файла", media)]
    if mode != "document":
        variants.append(("файлом", _build_inline_media(path, info, source_url, "document")))

    for name, item in variants:
        try:
            await _try_edit(bot, inline_message_id, item)
            log.info("inline: сработал вариант «%s»", name)
            await _drop_keyboard(bot, inline_message_id)
            return
        except TelegramBadRequest as exc:
            log.warning(
                "inline: вариант «%s» отклонён (%s, %s КБ): %s",
                name,
                path.suffix,
                path.stat().st_size // 1024,
                exc.message,
            )

    # не помогло — загружаем файл в личку и переиспользуем готовый file_id
    if user_id:
        sent = None
        try:
            sent, file_id = await _upload_for_file_id(bot, user_id, path, mode)
            item = _media_from_file_id(file_id, path, info, caption, mode)
            await _try_edit(bot, inline_message_id, item)
            log.info("inline: сработал вариант «file_id»")
            await _drop_keyboard(bot, inline_message_id)
            if sent is not None:
                try:
                    await sent.delete()
                except Exception:  # noqa: BLE001
                    pass
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("inline: вариант «file_id» не сработал: %s", exc)
            if sent is not None:
                sent = None  # файл уже у пользователя, удалять не нужно

    # совсем не вышло: честно пишем в сообщении, а файл отправляем в личку
    if user_id:
        await _send_to_dm(bot, user_id, path, info, mode)
        text = "✅ Файл отправил в личку с ботом."
    else:
        text = "❌ Не удалось показать файл здесь — вызовите бота в личке."
    await _safe_edit_text(bot, inline_message_id, text)


async def _safe_edit_text(bot: Bot, inline_message_id: str, text: str) -> None:

    try:
        await bot.edit_message_text(
            text=text, inline_message_id=inline_message_id, reply_markup=NO_KB
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("inline: не удалось даже текст поменять: %s", exc)


async def _upload_for_file_id(
    bot: Bot, user_id: int, path: Path, mode: str, caption: str = ""
) -> tuple[object, str]:
    """Отправляет файл в личку и возвращает сообщение + file_id."""
    ext = path.suffix.lower()
    file = FSInputFile(path, filename=safe_filename(path.stem, ext))
    caption = caption or None

    if ext in IMAGE_EXTS:
        message = await bot.send_photo(user_id, photo=file, caption=caption)
        file_id = message.photo[-1].file_id
    elif ext in AUDIO_EXTS:
        message = await bot.send_audio(user_id, audio=file, caption=caption)
        file_id = message.audio.file_id
    elif mode == "document":
        message = await bot.send_document(user_id, document=file, caption=caption)
        file_id = message.document.file_id
    else:
        message = await bot.send_video(
            user_id, video=file, caption=caption, supports_streaming=True
        )
        file_id = message.video.file_id
    return message, file_id


async def _send_to_dm(
    bot: Bot, user_id: int, path: Path, info: dict, mode: str, source_url: str = ""
) -> None:
    caption = f'<a href="{html.escape(source_url)}">ссылка</a>' if source_url else ""
    try:
        await _upload_for_file_id(bot, user_id, path, mode, caption)
    except Exception as exc:  # noqa: BLE001
        log.warning("inline: не удалось отправить файл в личку: %s", exc)


def _media_from_file_id(file_id: str, path: Path, info: dict, caption: str, mode: str):
    ext = path.suffix.lower()
    if mode == "document":
        return InputMediaDocument(media=file_id, caption=caption)
    if ext in AUDIO_EXTS:
        title = (info.get("track") or info.get("title") or path.stem)[:180]
        performer = (info.get("artist") or info.get("uploader") or "")[:120]
        return InputMediaAudio(
            media=file_id, caption=caption, title=title, performer=performer or None
        )
    if ext in IMAGE_EXTS:
        return InputMediaPhoto(media=file_id, caption=caption)
    return InputMediaVideo(media=file_id, caption=caption, supports_streaming=True)


async def _drop_keyboard(bot: Bot, inline_message_id: str) -> None:
    """Убирает кнопку «⬇️ Скачать» с уже готового сообщения (если она была)."""
    try:
        await bot.edit_message_reply_markup(
            inline_message_id=inline_message_id, reply_markup=NO_KB
        )
    except Exception:  # noqa: BLE001 — не критично
        pass


async def _try_edit(bot: Bot, inline_message_id: str, media) -> None:
    try:
        await bot.edit_message_media(
            media=media, inline_message_id=inline_message_id, reply_markup=NO_KB
        )
    except TelegramBadRequest as exc:
        if "invalid message content" not in exc.message.lower():
            raise
        # без клавиатуры Telegram принимает тот же контент
        await bot.edit_message_media(media=media, inline_message_id=inline_message_id)


def _build_inline_media(path: Path, info: dict, source_url: str, mode: str):
    ext = path.suffix.lower()
    title = (info.get("title") or path.stem)[:180]
    performer = (info.get("artist") or info.get("uploader") or "")[:120]
    duration = int(info.get("duration") or 0) or None
    caption = f'<a href="{html.escape(source_url)}">ссылка</a>'
    file = FSInputFile(path, filename=safe_filename(title, ext))

    if mode == "document":
        return InputMediaDocument(media=file, caption=caption)
    if ext in AUDIO_EXTS:
        return InputMediaAudio(
            media=file, caption=caption, title=title, performer=performer or None, duration=duration
        )
    if ext in IMAGE_EXTS:
        return InputMediaPhoto(media=file, caption=caption)
    return InputMediaVideo(
        media=file, caption=caption, duration=duration, supports_streaming=True
    )


async def _inline_error(bot: Bot, inline_message_id: str, text: str) -> None:
    try:
        me = await bot.me()
        hint = f"\n\nОткрыть бота: https://t.me/{me.username}" if me.username else ""
        await bot.edit_message_text(
            text=f"❌ {html.escape(text)}{hint}",
            inline_message_id=inline_message_id,
            disable_web_page_preview=True,
            reply_markup=NO_KB,
        )
    except Exception as exc:  # noqa: BLE001 — inline-сообщение могли удалить
        log.warning("cannot edit inline message with error: %s", exc)
