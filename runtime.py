"""Живые счётчики процесса: пригодится в /ping и в сторожевом таймере.

Render free засыпает без внешнего «будильника», а после простоя первый запрос
может потеряться. Чтобы отличить «бот спит» от «бот упал и перезапустился»,
держим время старта и время последнего полученного апдейта.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

STARTED_AT: float = time.time()

STATS: dict[str, float | int | str] = {
    "updates": 0,
    "messages": 0,
    "callbacks": 0,
    "inline": 0,
    "blocked": 0,
    "last_update": 0.0,
    "last_error": "",
    "webhook_check": 0.0,
    "webhook_error": "",
    "webhook_pending": 0,
}

# Тип апдейта Telegram → счётчик в STATS.
_KINDS = {
    "message": "messages",
    "edited_message": "messages",
    "callback_query": "callbacks",
    "inline_query": "inline",
    "chosen_inline_result": "inline",
}


def mark_update(kind: str) -> None:
    STATS["updates"] = int(STATS["updates"]) + 1
    STATS["last_update"] = time.time()
    counter = _KINDS.get(kind)
    if counter:
        STATS[counter] = int(STATS[counter]) + 1


def mark_blocked() -> None:
    STATS["blocked"] = int(STATS["blocked"]) + 1


def uptime() -> float:
    return time.time() - STARTED_AT


def human_age(seconds: float) -> str:
    """«12 сек», «5 мин», «3 ч 20 мин», «2 дн»."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)} сек"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} мин"
    hours = minutes // 60
    tail = minutes % 60
    if hours < 48:
        return f"{hours} ч {tail} мин" if tail else f"{hours} ч"
    return f"{hours // 24} дн"


def started_text() -> str:
    moment = datetime.fromtimestamp(STARTED_AT, tz=timezone.utc)
    return moment.strftime("%d.%m %H:%M UTC")


def last_update_text() -> str:
    stamp = float(STATS["last_update"] or 0)
    if not stamp:
        return "ещё не было"
    return f"{human_age(time.time() - stamp)} назад"
