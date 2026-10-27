"""Точка входа. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot, BaseMiddleware, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import storage
from config import (
    BOT_TOKEN,
    COOKIES_FILE,
    DOWNLOADS_DIR,
    FFMPEG_DIR,
    MAX_UPLOAD_MB,
    PORT,
    PUBLIC_URL,
    TELEGRAM_PROXY,
    WEBHOOK_SECRET,
)
from handlers import router
from inline import router as inline_router

log = logging.getLogger(__name__)

BASE_LOG = DOWNLOADS_DIR.parent / "bot.log"
WEBHOOK_PATH = "/webhook"


class _UpdateLogger(BaseMiddleware):
    """Пишет в лог тип каждого обновления — иначе непонятно, доходит ли
    chosen_inline_result (Telegram шлёт его только при включённом inline-фидбеке)."""

    async def __call__(self, handler, event, data):
        log.info("update: %s", getattr(event, "event_type", type(event).__name__))
        return await handler(event, data)


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

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(_UpdateLogger())
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
            ]
        )
    except TelegramNetworkError as exc:
        log.warning("не удалось обновить команды: %s", exc)

    storage.cleanup()
    log.info(
        "Бот запущен. Лимит отправки: %s МБ, ffmpeg: %s, cookies: %s, прокси: %s",
        MAX_UPLOAD_MB,
        FFMPEG_DIR or "не найден",
        "есть" if COOKIES_FILE else "нет",
        TELEGRAM_PROXY or "нет",
    )

    if PUBLIC_URL:
        await _run_webhook(bot, dp)
    else:
        await _run_polling(bot, dp)


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramNetworkError as exc:
        log.warning("не удалось снять вебхук: %s", exc)
    await dp.start_polling(bot)


async def _run_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Режим для PaaS: Telegram сам шлёт обновления на наш https-эндпоинт."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    secret = WEBHOOK_SECRET or None

    # Вебхук ставим до старта сервера: если Telegram недоступен, контейнер
    # упадёт и хостинг перезапустит его, а не будет делать вид, что всё хорошо.
    for attempt in range(1, 4):
        try:
            await bot.set_webhook(url, secret_token=secret, drop_pending_updates=True)
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

    # Нужно и для проверок хостинга, и для «пингеров», которые не дают
    # бесплатному контейнеру заснуть
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер слушает 0.0.0.0:%s", PORT)

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
