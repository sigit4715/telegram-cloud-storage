"""Per-Google-account Telegram Bot + private-channel configuration.

Secrets are encrypted at rest with a deployment-local key. This module deliberately
contains no Flask routes or Telethon client globals; callers must pass the authenticated
owner email and use get_client(owner) for all Telegram operations.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
import threading
from typing import Any

try:
    from cryptography.fernet import Fernet
except Exception as exc:  # pragma: no cover
    Fernet = None
    _FERNET_IMPORT_ERROR = exc

_CONFIG_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_clients: dict[str, Any] = {}
_clients_lock = threading.RLock()


def _owner(owner: str) -> str:
    value = str(owner or "").strip().lower()
    if not value or not _CONFIG_RE.match(value):
        raise ValueError("owner_email tidak valid")
    return value


def _fernet(key: str):
    if Fernet is None:
        raise RuntimeError("cryptography diperlukan untuk menyimpan credential Telegram") from _FERNET_IMPORT_ERROR
    raw = str(key or "").encode()
    digest = hashlib.sha256(raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def load_or_create_master_key(base_dir: str) -> str:
    """Load a deployment-local key, creating it once with owner-only permissions."""
    path = os.path.join(base_dir, ".telegram-config.key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if Fernet is None:
        raise RuntimeError("cryptography diperlukan untuk menyimpan credential Telegram") from _FERNET_IMPORT_ERROR
    key = Fernet.generate_key().decode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def _enc(value: str, key: str) -> str:
    return _fernet(key).encrypt(str(value).encode()).decode()


def _dec(value: str, key: str) -> str:
    return _fernet(key).decrypt(str(value).encode()).decode()


def init_telegram_accounts(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_accounts (
            owner_email TEXT PRIMARY KEY,
            api_id INTEGER NOT NULL,
            api_hash_enc TEXT NOT NULL,
            bot_token_enc TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            session_path TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()


def save_account_config(db_path: str, encryption_key: str, owner_email: str, values: dict[str, Any]) -> Any | None:
    owner = _owner(owner_email)
    try:
        api_id = int(str(values.get("api_id", "")).strip())
        channel_id = int(str(values.get("channel_id", "")).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("API ID dan Channel ID harus berupa angka") from exc
    if api_id <= 0 or not str(values.get("api_hash", "")).strip() or not str(values.get("bot_token", "")).strip() or channel_id == 0:
        # Empty secrets are handled below only when an existing row exists.
        existing = get_account_config(db_path, encryption_key, owner, allow_missing=True)
        if not existing or not str(values.get("api_hash", "")).strip() and not existing.get("api_hash") or not str(values.get("bot_token", "")).strip() and not existing.get("bot_token"):
            raise ValueError("API ID, API Hash, Bot Token, dan Channel ID wajib diisi")
    existing = get_account_config(db_path, encryption_key, owner, allow_missing=True)
    api_hash = str(values.get("api_hash", "")).strip() or (existing or {}).get("api_hash", "")
    bot_token = str(values.get("bot_token", "")).strip() or (existing or {}).get("bot_token", "")
    if not api_hash or not bot_token:
        raise ValueError("API Hash dan Bot Token wajib diisi")
    session_path = os.path.join(os.path.dirname(db_path), "telegram-sessions", hashlib.sha256(owner.encode()).hexdigest()[:32])
    os.makedirs(os.path.dirname(session_path), mode=0o700, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""INSERT INTO telegram_accounts
        (owner_email, api_id, api_hash_enc, bot_token_enc, channel_id, session_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_email) DO UPDATE SET api_id=excluded.api_id,
        api_hash_enc=excluded.api_hash_enc, bot_token_enc=excluded.bot_token_enc,
        channel_id=excluded.channel_id, session_path=excluded.session_path,
        updated_at=datetime('now','localtime')""",
        (owner, api_id, _enc(api_hash, encryption_key), _enc(bot_token, encryption_key), channel_id, session_path))
    conn.commit()
    conn.close()
    # Return the evicted owner-only client so the caller can disconnect it.
    return pop_client(owner)


def get_account_config(db_path: str, encryption_key: str, owner_email: str, allow_missing: bool = False) -> dict[str, Any] | None:
    owner = _owner(owner_email)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM telegram_accounts WHERE owner_email=?", (owner,)).fetchone()
    conn.close()
    if not row:
        if allow_missing:
            return None
        return None
    return {"owner_email": owner, "api_id": row["api_id"], "api_hash": _dec(row["api_hash_enc"], encryption_key), "bot_token": _dec(row["bot_token_enc"], encryption_key), "channel_id": row["channel_id"], "session_path": row["session_path"]}


def get_public_config(db_path: str, owner_email: str) -> dict[str, Any]:
    owner = _owner(owner_email)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT api_id, channel_id, api_hash_enc, bot_token_enc FROM telegram_accounts WHERE owner_email=?", (owner,)).fetchone()
    conn.close()
    if not row:
        return {"configured": False, "owner_email": owner, "api_id": "", "channel_id": "", "api_hash_set": False, "bot_token_set": False}
    return {"configured": True, "owner_email": owner, "api_id": str(row[0]), "channel_id": str(row[1]), "api_hash_set": bool(row[2]), "bot_token_set": bool(row[3])}


def migrate_legacy_config(db_path: str, encryption_key: str, owner_email: str, legacy: dict[str, Any]) -> None:
    if get_account_config(db_path, encryption_key, owner_email, allow_missing=True):
        return
    if not all(str(legacy.get(k, "")).strip() for k in ("API_ID", "API_HASH", "BOT_TOKEN", "CHANNEL")):
        return
    save_account_config(db_path, encryption_key, owner_email, {"api_id": legacy["API_ID"], "api_hash": legacy["API_HASH"], "bot_token": legacy["BOT_TOKEN"], "channel_id": legacy["CHANNEL"]})


def get_client(owner_email: str):
    """Return the cached client for owner; actual construction is injected by web.py."""
    owner = _owner(owner_email)
    with _clients_lock:
        return _clients.get(owner)


def set_client(owner_email: str, client: Any) -> None:
    with _clients_lock:
        _clients[_owner(owner_email)] = client


def pop_client(owner_email: str) -> Any | None:
    """Remove and return one owner's cached client without touching others."""
    with _clients_lock:
        return _clients.pop(_owner(owner_email), None)


def close_all_clients() -> None:
    with _clients_lock:
        _clients.clear()
