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
    """
)
_conn.commit()


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
