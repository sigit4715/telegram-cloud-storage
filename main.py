#!/usr/bin/env python3
"""
Telegram Cloud Storage - Level 2 (Telethon)
Private Channel sebagai "drive" + Bot sebagai interface.

Arsitektur:
  User --file--> Bot (Alfonsogm_bot) --upload--> Private Channel
  User --/get <id>--> Bot --> download dari Channel --> kirim balik

Requirements:
  pip install telethon
"""

import os
import sqlite3
import hashlib
import asyncio
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError

# ============================================================
# CONFIG  (isi dari config.env)
# ============================================================
BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, "config.env")

def load_env(path):
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

cfg = load_env(ENV_PATH)

API_ID = int(cfg.get("API_ID", "0"))
API_HASH = cfg.get("API_HASH", "")
BOT_TOKEN = cfg.get("BOT_TOKEN", "")
CHANNEL = cfg.get("CHANNEL", "me")
SESSION = "/root/telegram_bot_session"
DB_PATH = os.path.join(BASE, "storage.db")

# ============================================================
# DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            size INTEGER DEFAULT 0,
            mime TEXT,
            file_hash TEXT,
            uploaded_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(file_name)")
    conn.commit()
    conn.close()

def db_add(file_name, msg_id, size, mime, file_hash):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO files (file_name, msg_id, size, mime, file_hash) VALUES (?,?,?,?,?)",
        (file_name, msg_id, size, mime, file_hash),
    )
    conn.commit()
    conn.close()

def db_search(keyword):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, file_name, size, uploaded_at FROM files WHERE file_name LIKE ? ORDER BY id DESC",
        (f"%{keyword}%",),
    ).fetchall()
    conn.close()
    return rows

def db_get_by_id(fid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, file_name, msg_id, size, mime FROM files WHERE id=?", (fid,)
    ).fetchone()
    conn.close()
    return row

def db_list(limit=20):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, file_name, size, uploaded_at FROM files ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows

def db_delete(fid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT msg_id FROM files WHERE id=?", (fid,)).fetchone()
    conn.execute("DELETE FROM files WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return row[0] if row else None

def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

# ============================================================
# CLIENT
# ============================================================
client = TelegramClient(SESSION, API_ID, API_HASH)

# ============================================================
# ALLOWLIST MANAGEMENT
# ============================================================
# Allowed user IDs (from config)
ALLOWED = set(x.strip() for x in cfg.get("ALLOWED_USERS", "").split(",") if x.strip())
ADMIN_IDS = {"5337119189"}  # User utama (admin)

def is_allowed(user_id):
    """Cek apakah user ID diizinkan akses bot"""
    if not ALLOWED:  # Kalau kosong = semua boleh
        return True
    return str(user_id) in ALLOWED or str(user_id) in ADMIN_IDS

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

async def check_access(event):
    """Cek akses. Return True if allowed, False if denied."""
    sender = await event.get_sender()
    if not is_allowed(sender.id):
        await event.reply("⛔ **Akses Ditolak**\n\nAnda tidak terdaftar di sistem ini.")
        return False
    return True

# ============================================================
# ADMIN COMMANDS: /adduser <id> & /deluser <id> & /users
# ============================================================
@client.on(events.NewMessage(pattern=r"/adduser (\d+)"))
async def add_user(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("⛔ Hanya admin yang bisa menambah user.")
    new_id = event.pattern_match.group(1)
    ALLOWED.add(new_id)
    # Update config
    _save_allowed()
    await event.reply(f"✅ User `{new_id}` ditambahkan ke allowlist.", parse_mode="md")

@client.on(events.NewMessage(pattern=r"/deluser (\d+)"))
async def del_user(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("⛔ Hanya admin yang bisa menghapus user.")
    del_id = event.pattern_match.group(1)
    ALLOWED.discard(del_id)
    _save_allowed()
    await event.reply(f"🗑 User `{del_id}` dihapus dari allowlist.", parse_mode="md")

@client.on(events.NewMessage(pattern="/users"))
async def list_users(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("⛔ Hanya admin yang bisa melihat daftar user.")
    users = sorted(ALLOWED | ADMIN_IDS)
    text = "👥 **Daftar User yang Diizinkan**\n\n"
    for u in users:
        tag = " (admin)" if u in ADMIN_IDS else ""
        text += f"• `{u}`{tag}\n"
    text += f"\nTotal: {len(users)} user"
    await event.reply(text, parse_mode="md")

def _save_allowed():
    """Simpan allowlist ke config.env"""
    lines = []
    with open(ENV_PATH) as f:
        for line in f:
            if line.strip().startswith("ALLOWED_USERS"):
                continue
            lines.append(line)
    lines.append(f"ALLOWED_USERS={','.join(ALLOWED)}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)

# ============================================================
# BOT HANDLERS
# ============================================================
@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    if not await check_access(event):
        return
    await event.reply(
        "🗂 **Telegram Cloud Storage**\n\n"
        "Kirim file langsung ke chat ini untuk upload.\n\n"
        "Perintah:\n"
        "• `/list` — daftar file (20 terbaru)\n"
        "• `/get <id>` — download file by ID\n"
        "• `/search <kata>` — cari file\n"
        "• `/del <id>` — hapus file\n"
        "• `/stats` — info storage\n"
        "• `/adduser <id>` — tambah user (admin)\n"
        "• `/deluser <id>` — hapus user (admin)\n"
        "• `/users` — daftar user (admin)",
        parse_mode="md",
    )

@client.on(events.NewMessage(pattern="/list"))
async def list_files(event):
    if not await check_access(event):
        return
    rows = db_list()
    if not rows:
        await event.reply("📭 Belum ada file.")
        return
    text = "📂 **Daftar File**\n\n"
    for r in rows:
        fid, name, size, ts = r
        text += f"`{fid}` • {name} • {human_size(size)}\n"
    await event.reply(text, parse_mode="md")

@client.on(events.NewMessage(pattern=r"/get (\d+)"))
async def get_file(event):
    if not await check_access(event):
        return
    fid = int(event.pattern_match.group(1))
    row = db_get_by_id(fid)
    if not row:
        await event.reply("❌ ID tidak ditemukan.")
        return
    _, name, msg_id, size, mime = row
    await event.reply("⏳ Mengambil file...")
    try:
        msg = await client.get_messages(CHANNEL, ids=msg_id)
        if not msg or not msg.media:
            await event.reply("❌ File di channel tidak ditemukan (mungkin sudah dihapus).")
            return
        await client.send_file(
            event.chat_id,
            msg.media,
            caption=f"📎 `{name}` • {human_size(size)}",
            parse_mode="md",
        )
    except FloodWaitError as e:
        await event.reply(f"⏳ Terkena rate-limit, coba lagi dalam {e.seconds}s.")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

@client.on(events.NewMessage(pattern=r"/search (.+)"))
async def search_file(event):
    if not await check_access(event):
        return
    kw = event.pattern_match.group(1)
    rows = db_search(kw)
    if not rows:
        await event.reply(f"🔍 Tidak ada hasil untuk '{kw}'.")
        return
    text = f"🔍 **Hasil untuk '{kw}'**\n\n"
    for r in rows:
        fid, name, size, ts = r
        text += f"`{fid}` • {name} • {human_size(size)}\n"
    await event.reply(text, parse_mode="md")

@client.on(events.NewMessage(pattern=r"/del (\d+)"))
async def del_file(event):
    if not await check_access(event):
        return
    fid = int(event.pattern_match.group(1))
    msg_id = db_delete(fid)
    if msg_id is None:
        await event.reply("❌ ID tidak ditemukan.")
        return
    try:
        await client.delete_messages(CHANNEL, [msg_id])
    except Exception:
        pass
    await event.reply(f"🗑 File ID `{fid}` dihapus.", parse_mode="md")

@client.on(events.NewMessage(pattern="/stats"))
async def stats(event):
    if not await check_access(event):
        return
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
    conn.close()
    await event.reply(
        f"📊 **Storage Stats**\n\n"
        f"Total file: `{total[0]}`\n"
        f"Total ukuran: `{human_size(total[1])}`\n"
        f"Channel: `{CHANNEL}`",
        parse_mode="md",
    )

@client.on(events.NewMessage(func=lambda e: e.media is not None and not e.out))
async def upload_handler(event):
    """User kirim file → upload ke Private Channel → index di DB."""
    if not await check_access(event):
        return

    await event.reply("⏳ Mengupload ke storage...")

    # Dapatkan nama file
    name = None
    if event.document and event.document.attributes:
        for attr in event.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                name = attr.file_name
    if not name:
        name = f"file_{event.message.id}"

    size = event.document.size if event.document else 0
    mime = event.document.mime_type if event.document else None

    try:
        forwarded = await client.send_file(
            CHANNEL,
            event.media,
            caption=f"cloud:{name}",
        )
        msg_id = forwarded.id
        fh = hashlib.md5(name.encode()).hexdigest()[:12]
        db_add(name, msg_id, size, mime, fh)
        await event.reply(
            f"✅ **Tersimpan**\n\n"
            f"Nama: `{name}`\n"
            f"Ukuran: `{human_size(size)}`\n"
            f"ID: `{msg_id}`",
            parse_mode="md",
        )
    except FloodWaitError as e:
        await event.reply(f"⏳ Terkena rate-limit, coba lagi dalam {e.seconds}s.")
    except Exception as e:
        await event.reply(f"❌ Upload gagal: {e}")

# ============================================================
# MAIN
# ============================================================
async def main():
    init_db()
    print("[*] Menghubungkan ke Telegram...")
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[*] Login sebagai: {me.username or me.first_name} (id={me.id})")
    print(f"[*] Cloud storage siap. Tekan Ctrl+C untuk berhenti.")
    print(f"[*] Allowed users: {ALLOWED or 'ALL'}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[*] Dihentikan.")
    finally:
        client.disconnect()
