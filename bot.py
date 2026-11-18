"""Точка входа. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path

from aiogram import Bot, BaseMiddleware, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineQuery,
    Message,
    Update,
)

import runtime
import storage
from config import (
    BOT_TOKEN,
    BUILD,
    COBALT_API,
    COBALT_SERVICES,
    COBALT_YOUTUBE,
    COOKIES_FILE,
    DOWNLOADS_DIR,
    FFMPEG_DIR,
    MAX_UPLOAD_MB,
    OWNER_IDS,
    PORT,
    PRIVATE_TEXT,
    PROXY,
    PUBLIC_URL,
    TELEGRAM_PROXY,
    WEBHOOK_SECRET,
    YT_PROXY,
    is_owner,
)
from downloader import js_runtime_report
from handlers import router
from inline import router as inline_router

log = logging.getLogger(__name__)

BASE_LOG = DOWNLOADS_DIR.parent / "bot.log"
WEBHOOK_PATH = "/webhook"


class _UpdateLogger(BaseMiddleware):
    """Пишет в лог тип каждого обновления — иначе непонятно, доходит ли
    chosen_inline_result (Telegram шлёт его только при включённом inline-фидбеке)."""

    async def __call__(self, handler, event, data):
        kind = getattr(event, "event_type", type(event).__name__)
        runtime.mark_update(str(kind))
        log.info("update: %s", kind)
        return await handler(event, data)


class _AccessMiddleware(BaseMiddleware):
    """Приватный режим: всё, что приходит не от OWNER_IDS, вежливо отбиваем.

    Стоит внешним middleware на dp.update, поэтому закрывает и сообщения,
    и кнопки, и inline-режим — включая чужие chosen_inline_result.
    """

    async def __call__(self, handler, event: Update, data):
        try:
            inner = event.event
        except ValueError:  # пустой апдейт — нечего проверять
            return await handler(event, data)
        user = getattr(inner, "from_user", None)
        if is_owner(user.id if user else None):
            return await handler(event, data)

        runtime.mark_blocked()
        log.info(
            "доступ закрыт: %s от %s (@%s)",
            getattr(inner, "event_type", type(inner).__name__),
            user.id if user else "?",
            user.username if user and user.username else "—",
        )
        try:
            if isinstance(inner, Message):
                text = PRIVATE_TEXT
                if user:
                    text += (
                        f"\n\nВаш ID: <code>{user.id}</code> — добавьте его в "
                        "OWNER_IDS, если это ваш бот."
                    )
                await inner.answer(text)
            elif isinstance(inner, CallbackQuery):
                alert = PRIVATE_TEXT
                if user:
                    alert += f"\nВаш ID: {user.id}"
                await inner.answer(alert, show_alert=True)
            elif isinstance(inner, InlineQuery):
                await inner.answer([], cache_time=0, is_personal=True)
        except Exception as exc:  # noqa: BLE001 — отказ не должен валить обработчик
            log.debug("не удалось ответить на чужой апдейт: %s", exc)
        return None


def _prepare_downloads_dir() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # подчищаем только свои временные папки (mkdtemp создаёт tmpXXXX) от прошлых запусков
    for item in DOWNLOADS_DIR.iterdir():
        if item.is_dir() and item.name.startswith("tmp"):
            shutil.rmtree(item, ignore_errors=True)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан.\n"
            "Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    if not FFMPEG_DIR:
        log.warning("ffmpeg не найден — не будет конвертации в mp3 и склейки видео.")

    _prepare_downloads_dir()

    if COOKIES_FILE:
        cookie_path = Path(COOKIES_FILE)
        if cookie_path.exists():
            log.info("cookies: %s (%s байт)", COOKIES_FILE, cookie_path.stat().st_size)
        else:
            log.warning(
                "cookies: файл %s не найден — YouTube может требовать вход", COOKIES_FILE
            )

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(_UpdateLogger())
    dp.update.outer_middleware(_AccessMiddleware())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Не засоряем логи тем, что и так ожидаемо: старые query_id и прочее."""
        exc = event.exception
        if isinstance(exc, TelegramBadRequest) and "query is too old" in str(exc):
            log.info("апдейт устарел, ответ не нужен: %s", exc)
            return True
        log.error("необработанная ошибка: %s", exc, exc_info=exc)
        return True

    dp.include_router(router)
    dp.include_router(inline_router)

    me = None
    for attempt in range(1, 7):
        try:
            me = await bot.get_me()
            break
        except TelegramUnauthorizedError:
            raise SystemExit("Токен не подошёл. Проверьте BOT_TOKEN в .env.") from None
        except TelegramNetworkError as exc:
            log.warning("нет связи с Telegram (попытка %s/6): %s", attempt, exc)
            if attempt == 6:
                raise SystemExit(
                    "Не могу подключиться к api.telegram.org.\n"
                    "Похоже, доступ заблокирован или VPN не работает. Включите VPN "
                    "(TUN-режим) или пропишите TELEGRAM_PROXY в .env."
                ) from None
            await asyncio.sleep(5 * attempt)

    log.info("Подключился как @%s", me.username if me else "?")

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Что я умею"),
                BotCommand(command="help", description="Помощь"),
                BotCommand(command="settings", description="Настройки"),
                BotCommand(command="services", description="Доступные сервисы"),
                BotCommand(command="ping", description="Жив ли бот и доставка"),
                BotCommand(command="id", description="Мой Telegram ID"),
                BotCommand(command="yt_debug", description="Диагностика YouTube"),
            ]
        )
    except TelegramNetworkError as exc:
        log.warning("не удалось обновить команды: %s", exc)

    storage.cleanup()
    if OWNER_IDS:
        log.info("приватный режим: доступ у %s", ", ".join(str(i) for i in sorted(OWNER_IDS)))
    else:
        log.warning(
            "OWNER_IDS не задан — бот открыт всем. Свой ID покажет /id, "
            "потом добавьте OWNER_IDS=<id> в Environment."
        )
    log.info(
        "Бот запущен (сборка %s). Лимит отправки: %s МБ, ffmpeg: %s, cookies: %s, "
        "cobalt: %s (%s), youtube: %s, %s, прокси Telegram: %s, прокси YouTube: %s",
        BUILD,
        MAX_UPLOAD_MB,
        FFMPEG_DIR or "не найден",
        "есть" if COOKIES_FILE else "нет",
        "есть" if COBALT_API else "нет",
        COBALT_SERVICES or "—",
        "cobalt" if (COBALT_API and COBALT_YOUTUBE) else "yt-dlp",
        js_runtime_report(),
        TELEGRAM_PROXY or "нет",
        YT_PROXY or PROXY or "нет",
    )

    if PUBLIC_URL:
        if not WEBHOOK_SECRET:
            log.warning(
                "WEBHOOK_SECRET не задан — вебхук принимает запросы без проверки. "
                "Задайте WEBHOOK_SECRET в Environment, чтобы защитить /webhook "
                "от поддельных обновлений."
            )
        await _run_webhook(bot, dp)
    else:
        await _run_polling(bot, dp)


_BG_TASKS: set[asyncio.Task] = set()


def _start_watchdog(bot: Bot) -> None:
    """Держим ссылку на задачу: event loop хранит только слабые ссылки."""
    task = asyncio.create_task(_watchdog(bot))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _watchdog(bot: Bot) -> None:
    """Сторож доставки: раз в 5 минут смотрит, что Telegram не копит апдейты.

    Render free засыпает после ~15 минут без входящих запросов. Если внешний
    «будильник» перестал стучать, первый апдейт приходит с задержкой; а если
    очередь начала расти или вебхук отвалился — переставляем его сами.
    """
    while True:
        await asyncio.sleep(300)
        try:
            info = await bot.get_webhook_info()
        except Exception as exc:  # noqa: BLE001 — сторож не должен падать
            log.warning("сторож: get_webhook_info не ответил: %s", exc)
            continue

        err = info.last_error_message or ""
        runtime.STATS["webhook_check"] = time.time()
        runtime.STATS["webhook_pending"] = info.pending_update_count or 0
        runtime.STATS["webhook_error"] = err
        log.info(
            "сторож: вебхук %s, в очереди %s, ошибка: %s",
            info.url or "нет",
            info.pending_update_count or 0,
            err or "нет",
        )

        fresh_error = False
        if err and info.last_error_date is not None:
            try:
                stamp = info.last_error_date.timestamp()
            except AttributeError:
                stamp = float(info.last_error_date)
            fresh_error = time.time() - stamp < 1800

        if not (fresh_error or not info.url) or not PUBLIC_URL:
            continue
        try:
            await bot.set_webhook(
                f"{PUBLIC_URL}{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=False,
            )
            log.warning("сторож: вебхук переставлен на %s", PUBLIC_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("сторож: не удалось переставить вебхук: %s", exc)


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    try:
        # drop_pending_updates=True терял сообщения, написанные во время
        # перезапуска/холодного старта. Лучше доставить их с опозданием.
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramNetworkError as exc:
        log.warning("не удалось снять вебхук: %s", exc)
    _start_watchdog(bot)
    await dp.start_polling(bot)


async def _run_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Режим для PaaS: Telegram сам шлёт обновления на наш https-эндпоинт."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    secret = WEBHOOK_SECRET or None

    # Вебхук ставим до старта сервера: если Telegram недоступен, контейнер
    # упадёт и хостинг перезапустит его, а не будет делать вид, что всё хорошо.
    # drop_pending_updates=False: сообщения, написанные во время холодного
    # старта Render, не выбрасываем — Telegram дошлёт их, когда поднимемся.
    for attempt in range(1, 4):
        try:
            await bot.set_webhook(url, secret_token=secret, drop_pending_updates=False)
            log.info("Вебхук установлен: %s", url)
            break
        except TelegramNetworkError as exc:
            log.warning("не удалось поставить вебхук (%s/3): %s", attempt, exc)
            if attempt == 3:
                raise SystemExit("Telegram недоступен, вебхук не установлен.") from exc
            await asyncio.sleep(5 * attempt)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def status(_request: web.Request) -> web.Response:
        """Для /ping из браузера и мониторинга: жив ли бот и когда был апдейт."""
        return web.json_response(
            {
                "ok": True,
                "build": BUILD,
                "uptime": runtime.human_age(runtime.uptime()),
                "updates": runtime.STATS["updates"],
                "blocked": runtime.STATS["blocked"],
                "last_update": runtime.last_update_text(),
                "webhook_pending": runtime.STATS["webhook_pending"],
                "webhook_error": runtime.STATS["webhook_error"],
            }
        )

    # Нужно и для проверок хостинга, и для «пингеров», которые не дают
    # бесплатному контейнеру заснуть
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер слушает 0.0.0.0:%s", PORT)
    _start_watchdog(bot)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_LOG, encoding="utf-8"),
        ],
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        if exc.code:
            print(exc.code, file=sys.stderr)
