"""Мини-хранилище на SQLite: кэш file_id и отложенные запросы для inline-режима.

Зачем: inline-бот не может «скачать и отправить» — он обязан ответить мгновенно.
Поэтому всё, что бот когда-либо скачал, запоминается по file_id и потом отдаётся
в любой чат одной кнопкой.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from urllib.parse import parse_qs, urlsplit, urlunsplit

from config import BASE_DIR

DB_PATH = BASE_DIR / "cache.db"

MEDIA_TTL = 30 * 24 * 3600  # месяц
PENDING_TTL = 2 * 3600  # ссылка из inline-режима живёт 2 часа

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS media (
        key        TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        file_id    TEXT NOT NULL,
        title      TEXT DEFAULT '',
        thumb      TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pending (
        token      TEXT PRIMARY KEY,
        payload    TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        user_id    TEXT PRIMARY KEY,
        payload    TEXT NOT NULL
    );
    """
)
_conn.commit()

# Настройки по умолчанию: как бот вёл себя до появления /settings.
DEFAULT_SETTINGS: dict[str, bool] = {
    "send_as_file": False,  # False — «обычно», True — документом без сжатия
    "keep_source": True,  # оставлять ссылку на источник в подписи
}


def normalize_url(url: str) -> str:
    """Один и тот же ролик в разных видах ссылок должен иметь один ключ кэша."""
    url = url.strip()
    parts = urlsplit(url)
    host = parts.netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    path = parts.path.rstrip("/")

    if host == "youtu.be":
        vid = path.lstrip("/")
        return f"https://youtu.be/{vid}" if vid else url

    if host.endswith("youtube.com"):
        qs = parse_qs(parts.query)
        if qs.get("v"):
            return f"https://youtu.be/{qs['v'][0]}"
        if path.startswith("/shorts/"):
            return f"https://youtu.be/{path.split('/')[2]}"

    # для TikTok/Twitter/Instagram трекинг-параметры не нужны
    return urlunsplit(("https", host, path, "", ""))


def save_media(key: str, kind: str, file_id: str, title: str = "", thumb: str | None = None) -> None:
    _conn.execute(
        "INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?)",
        (key, kind, file_id, title or "", thumb, time.time()),
    )
    _conn.commit()


def get_media(key: str) -> dict | None:
    row = _conn.execute("SELECT * FROM media WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    if time.time() - row["created_at"] > MEDIA_TTL:
        return None
    return dict(row)


def search_media(text: str, limit: int = 8) -> list[dict]:
    """Ищет уже скачанные файлы по названию (для inline-режима)."""
    cleaned = text.replace("%", "").replace("_", " ").strip()
    if not cleaned:
        return []
    rows = _conn.execute(
        "SELECT * FROM media WHERE kind = 'audio' AND title LIKE ? "
        "ORDER BY created_at DESC LIMIT ?",
        (f"%{cleaned}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def save_pending(payload: dict, token: str | None = None) -> str:
    token = token or uuid.uuid4().hex[:16]
    _conn.execute(
        "INSERT OR REPLACE INTO pending VALUES (?, ?, ?)",
        (token, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    _conn.commit()
    return token


def pop_pending(token: str) -> dict | None:
    row = _conn.execute(
        "SELECT payload, created_at FROM pending WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    _conn.execute("DELETE FROM pending WHERE token = ?", (token,))
    _conn.commit()
    if time.time() - row["created_at"] > PENDING_TTL:
        return None
    return json.loads(row["payload"])


def cleanup() -> None:
    now = time.time()
    _conn.execute("DELETE FROM pending WHERE created_at < ?", (now - PENDING_TTL,))
    _conn.execute("DELETE FROM media WHERE created_at < ?", (now - MEDIA_TTL,))
    _conn.commit()


# ---------------------------------------------------------------- настройки


def get_settings(user_id: int | str | None) -> dict[str, bool]:
    """Настройки пользователя; по умолчанию — поведение без /settings."""
    settings = dict(DEFAULT_SETTINGS)
    if user_id is None:
        return settings
    row = _conn.execute(
        "SELECT payload FROM settings WHERE user_id = ?", (str(user_id),)
    ).fetchone()
    if not row:
        return settings
    try:
        saved = json.loads(row["payload"])
    except Exception:  # noqa: BLE001 — битая запись не должна ломать бота
        return settings
    if isinstance(saved, dict):
        for key in DEFAULT_SETTINGS:
            if key in saved:
                settings[key] = bool(saved[key])
    return settings


def save_settings(user_id: int | str, settings: dict) -> dict[str, bool]:
    merged = {
        key: bool(settings.get(key, default)) for key, default in DEFAULT_SETTINGS.items()
    }
    _conn.execute(
        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
        (str(user_id), json.dumps(merged)),
    )
    _conn.commit()
    return merged


def toggle_setting(user_id: int | str, key: str) -> bool:
    """Переключает настройку и возвращает новое значение."""
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    settings = get_settings(user_id)
    settings[key] = not settings[key]
    save_settings(user_id, settings)
    return settings[key]
