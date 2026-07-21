#!/usr/bin/env python3
"""
Telegram Cloud Storage - Web Dashboard v2
Features: Folder, Multi-Upload, Progress Bar, Real-time, Success/Fail indicators
"""

import os
import io
import sqlite3
import hashlib
import asyncio
import threading
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, url_for, session
from urllib.parse import urlencode
from werkzeug.middleware.proxy_fix import ProxyFix
from web_drive import register_drive_features
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError

# ============================================================
# CONFIG
# ============================================================
BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, "config.env")
DB_PATH = os.path.join(BASE, "storage.db")
ICONS_PATH = os.path.join(BASE, "icons", "cloud-storage-icons")

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
CHANNEL = int(cfg.get("CHANNEL", "0"))
SESSION = "/root/telegram_web_session"

ALLOWED = set(x.strip() for x in cfg.get("ALLOWED_USERS", "").split(",") if x.strip())
GOOGLE_ALLOWED_EMAILS = set(x.strip().lower() for x in cfg.get("GOOGLE_ALLOWED_EMAILS", "").split(",") if x.strip())
if not GOOGLE_ALLOWED_EMAILS:
    GOOGLE_ALLOWED_EMAILS = {"bowor4751@gmail.com"}
ADMIN_IDS = {"5337119189"}

# ============================================================
# TELETHON (async bridge)
# ============================================================
telethon_client = None
_loop = None
_loop_lock = threading.Lock()

def get_loop():
    global _loop
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                _loop = asyncio.new_event_loop()
                threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, get_loop()).result(timeout=120)

async def _init_telethon():
    global telethon_client
    telethon_client = TelegramClient(SESSION, API_ID, API_HASH)
    await telethon_client.start(bot_token=BOT_TOKEN)
    me = await telethon_client.get_me()
    print(f"[Telethon] Bot: @{me.username} id={me.id}")

# ============================================================
# DATABASE
# ============================================================
def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def db_exec(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

def db_scalar(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute(sql, params).fetchone()
    conn.close()
    return r[0] if r else None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Folders table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(name, parent_id)
        )
    """)
    # Files table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            size INTEGER DEFAULT 0,
            mime TEXT,
            file_hash TEXT,
            folder_id INTEGER DEFAULT 0,
            uploaded_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # Migrate optional navigation state columns without disturbing existing files.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    if "folder_id" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER DEFAULT 0")
    if "is_favorite" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN is_favorite INTEGER DEFAULT 0")
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN deleted_at TEXT DEFAULT NULL")
    conn.commit()
    conn.close()

def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
app.config["PREFERRED_URL_SCHEME"] = "https"
app.secret_key = "telegram-cloud-storage-2026"
# Cloudflare tunnel: Flask must trust X-Forwarded-* headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
# Session cookie: must survive HTTPS redirect from Google OAuth
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        g_email = session.get("google_email", "").lower()
        is_allowed = (uid and uid in ALLOWED) or (uid and uid.lower() in GOOGLE_ALLOWED_EMAILS) or (g_email and g_email in GOOGLE_ALLOWED_EMAILS)
        if not uid or (ALLOWED and not is_allowed and uid not in ADMIN_IDS):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper

# ============================================================
# AUTH API
# ============================================================
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    uid = str(data.get("user_id", "")).strip()
    if not uid:
        return jsonify({"error": "ID wajib diisi"}), 400
    is_allowed = (uid in ALLOWED) or (uid.lower() in GOOGLE_ALLOWED_EMAILS)
    if not is_allowed and uid not in ADMIN_IDS:
        return jsonify({"error": "ID tidak terdaftar"}), 403
    session["user_id"] = uid
    return jsonify({"ok": True, "user_id": uid})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def api_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False}), 401
    return jsonify({"logged_in": True, "user_id": uid, "is_admin": uid in ADMIN_IDS})

# ============================================================
# FOLDER API
# ============================================================
@app.route("/api/folders", methods=["GET"])
@login_required
def api_folders_list():
    parent = int(request.args.get("parent_id", 0))
    rows = db_query("""
        SELECT f.id, f.name, f.created_at,
               (SELECT COUNT(*) FROM files x WHERE x.folder_id=f.id) +
               (SELECT COUNT(*) FROM folders y WHERE y.parent_id=f.id) AS item_count
        FROM folders f WHERE f.parent_id=? ORDER BY f.name
    """, (parent,))
    return jsonify({"folders": [{"id": r["id"], "name": r["name"], "created": r["created_at"], "item_count": r["item_count"]} for r in rows]})

@app.route("/api/folders", methods=["POST"])
@login_required
def api_folders_create():
    data = request.get_json()
    name = data.get("name", "").strip()
    parent_id = int(data.get("parent_id", 0))
    if not name:
        return jsonify({"error": "Nama folder wajib"}), 400
    try:
        db_exec("INSERT INTO folders (name, parent_id) VALUES (?,?)", (name, parent_id))
        fid = db_scalar("SELECT last_insert_rowid()")
        return jsonify({"ok": True, "id": fid, "name": name})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Folder sudah ada"}), 409

@app.route("/api/folders/<int:fid>", methods=["DELETE"])
@login_required
def api_folders_delete(fid):
    # Delete children recursively
    def _delete_children(parent_id):
        children = db_query("SELECT id FROM folders WHERE parent_id=?", (parent_id,))
        for c in children:
            _delete_children(c["id"])
        db_exec("DELETE FROM folders WHERE parent_id=?", (parent_id,))
        # Delete files in this folder
        file_rows = db_query("SELECT id, msg_id FROM files WHERE folder_id=?", (parent_id,))
        for fr in file_rows:
            try:
                run_async(telethon_client.delete_messages(CHANNEL, [fr["msg_id"]]))
            except:
                pass
            db_exec("DELETE FROM files WHERE id=?", (fr["id"],))

    _delete_children(fid)
    # Delete files in root of this folder
    file_rows = db_query("SELECT id, msg_id FROM files WHERE folder_id=?", (fid,))
    for fr in file_rows:
        try:
            run_async(telethon_client.delete_messages(CHANNEL, [fr["msg_id"]]))
        except:
            pass
        db_exec("DELETE FROM files WHERE id=?", (fr["id"],))
    db_exec("DELETE FROM folders WHERE id=?", (fid,))
    return jsonify({"ok": True})

@app.route("/api/folders/breadcrumb/<int:fid>")
@login_required
def api_breadcrumb(fid):
    path = []
    current = fid
    while current > 0:
        row = db_query("SELECT id, name, parent_id FROM folders WHERE id=?", (current,))
        if not row:
            break
        r = row[0]
        path.insert(0, {"id": r["id"], "name": r["name"]})
        current = r["parent_id"]
    return jsonify({"breadcrumb": path})

# ============================================================
# FILE API
# ============================================================
def _page_params():
    page = max(1, int(request.args.get("page", 1)))
    per_page = max(10, min(100, int(request.args.get("per_page", 25))))
    return page, per_page, (page - 1) * per_page

def _file_json(rows):
    files = []
    for r in rows:
        mime = r["mime"] or ""
        files.append({"id": r["id"], "name": r["file_name"], "size": r["size"],
                      "size_human": human_size(r["size"]), "mime": mime,
                      "uploaded": r["uploaded_at"], "folder_id": r["folder_id"],
                      "is_favorite": bool(r["is_favorite"]),
                      "is_image": mime.startswith("image/"), "is_video": mime.startswith("video/"),
                      "is_audio": mime.startswith("audio/"), "is_pdf": mime == "application/pdf"})
    return files

@app.route("/api/files")
@login_required
def api_files():
    q = request.args.get("q", "").strip()
    folder_id = int(request.args.get("folder_id", 0))
    page, per_page, offset = _page_params()

    params = []
    where = "WHERE folder_id=?"
    params.append(folder_id)

    if q:
        where += " AND file_name LIKE ?"
        params.append(f"%{q}%")

    total = db_scalar(f"SELECT COUNT(*) FROM files {where}", params)
    rows = db_query(
        f"SELECT id, file_name, size, mime, uploaded_at, folder_id, is_favorite FROM files {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )

    files = []
    for r in rows:
        files.append({
            "id": r["id"],
            "name": r["file_name"],
            "size": r["size"],
            "size_human": human_size(r["size"]),
            "mime": r["mime"] or "",
            "uploaded": r["uploaded_at"],
            "folder_id": r["folder_id"],
            "is_favorite": bool(r["is_favorite"]),
            "is_image": (r["mime"] or "").startswith("image/"),
            "is_video": (r["mime"] or "").startswith("video/"),
            "is_audio": (r["mime"] or "").startswith("audio/"),
            "is_pdf": (r["mime"] or "") == "application/pdf",
            "has_thumb": (r["mime"] or "").startswith("image/"),
        })

    return jsonify({
        "files": files,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })

@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    files = request.files.getlist("files")
    folder_id = int(request.form.get("folder_id", 0))
    if not files:
        return jsonify({"error": "No files"}), 400

    results = []
    for f in files:
        if not f.filename:
            continue
        name = f.filename
        data = f.read()
        size = len(data)
        mime = f.content_type or "application/octet-stream"
        fh = hashlib.md5(name.encode()).hexdigest()[:12]

        try:
            is_img = mime.startswith('image/')
            forwarded = run_async(telethon_client.send_file(
                CHANNEL,
                file=data,
                caption=f"cloud:{folder_id}:{name}",
                force_document=not is_img,
            ))
            msg_id = forwarded.id
            db_exec(
                "INSERT INTO files (file_name, msg_id, size, mime, file_hash, folder_id) VALUES (?,?,?,?,?,?)",
                (name, msg_id, size, mime, fh, folder_id),
            )
            results.append({"name": name, "size": size, "size_human": human_size(size), "ok": True})
        except FloodWaitError as e:
            results.append({"name": name, "error": f"Rate limit {e.seconds}s", "ok": False})
        except Exception as e:
            results.append({"name": name, "error": str(e), "ok": False})

    return jsonify({"results": results})

@app.route("/api/download/<int:fid>")
@login_required
def api_download(fid):
    row = db_query("SELECT file_name, msg_id, mime FROM files WHERE id=?", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    r = row[0]
    try:
        msg = run_async(telethon_client.get_messages(CHANNEL, ids=r["msg_id"]))
        if not msg or not msg.media:
            return jsonify({"error": "File removed"}), 404
        buf = io.BytesIO()
        run_async(telethon_client.download_media(msg.media, file=buf))
        buf.seek(0)
        return send_file(buf, mimetype=r["mime"] or "application/octet-stream",
                         as_attachment=True, download_name=r["file_name"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/preview/<int:fid>")
@login_required
def api_preview(fid):
    row = db_query("SELECT file_name, msg_id, mime FROM files WHERE id=?", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    r = row[0]
    try:
        msg = run_async(telethon_client.get_messages(CHANNEL, ids=r["msg_id"]))
        if not msg or not msg.media:
            return jsonify({"error": "File removed"}), 404
        buf = io.BytesIO()
        run_async(telethon_client.download_media(msg.media, file=buf))
        buf.seek(0)
        return send_file(buf, mimetype=r["mime"] or "application/octet-stream")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete/<int:fid>", methods=["DELETE"])
@login_required
def api_delete(fid):
    # Normal delete is reversible: keep the Telegram file intact until explicitly purged.
    row = db_query("SELECT id FROM files WHERE id=? AND deleted_at IS NULL", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    db_exec("UPDATE files SET deleted_at=datetime('now','localtime') WHERE id=?", (fid,))
    return jsonify({"ok": True, "trashed": True})

@app.route("/api/navigation/<section>")
@login_required
def api_navigation(section):
    section = section.lower()
    clauses = []
    if section == "recent":
        clauses.append("deleted_at IS NULL")
    elif section == "favorites":
        clauses.extend(["deleted_at IS NULL", "is_favorite=1"])
    elif section == "trash":
        clauses.append("deleted_at IS NOT NULL")
    elif section == "shared":
        return jsonify({"section": section, "files": [], "total": 0, "page": 1, "per_page": 25, "pages": 1})
    else:
        return jsonify({"error": "unknown section"}), 404
    params = []
    q = request.args.get("q", "").strip()
    if q:
        clauses.append("file_name LIKE ?")
        params.append(f"%{q}%")
    page, per_page, offset = _page_params()
    where = " WHERE " + " AND ".join(clauses)
    total = db_scalar("SELECT COUNT(*) FROM files" + where, params) or 0
    rows = db_query("SELECT id, file_name, size, mime, uploaded_at, folder_id, is_favorite FROM files" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [per_page, offset])
    pages = max(1, (total + per_page - 1) // per_page)
    return jsonify({"section": section, "files": _file_json(rows), "total": total,
                    "page": page, "per_page": per_page, "pages": pages})

@app.route("/api/files/<int:fid>/favorite", methods=["POST"])
@login_required
def api_toggle_favorite(fid):
    row = db_query("SELECT id, is_favorite FROM files WHERE id=? AND deleted_at IS NULL", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    value = 0 if row[0]["is_favorite"] else 1
    db_exec("UPDATE files SET is_favorite=? WHERE id=?", (value, fid))
    return jsonify({"ok": True, "is_favorite": bool(value)})

@app.route("/api/trash/<int:fid>", methods=["POST"])
@login_required
def api_move_to_trash(fid):
    row = db_query("SELECT id FROM files WHERE id=? AND deleted_at IS NULL", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    db_exec("UPDATE files SET deleted_at=datetime('now','localtime') WHERE id=?", (fid,))
    return jsonify({"ok": True})

@app.route("/api/trash/<int:fid>/restore", methods=["POST"])
@login_required
def api_restore_from_trash(fid):
    row = db_query("SELECT id FROM files WHERE id=? AND deleted_at IS NOT NULL", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    db_exec("UPDATE files SET deleted_at=NULL WHERE id=?", (fid,))
    return jsonify({"ok": True})

@app.route("/api/files/type/<kind>")
@login_required
def api_files_by_type(kind):
    kind = kind.lower()
    clauses = ["deleted_at IS NULL"]
    params = []
    if kind == "document":
        clauses.append("(lower(file_name) LIKE '%.doc' OR lower(file_name) LIKE '%.docx' OR lower(file_name) LIKE '%.txt' OR lower(file_name) LIKE '%.rtf')")
    elif kind == "spreadsheet":
        clauses.append("(lower(file_name) LIKE '%.xls' OR lower(file_name) LIKE '%.xlsx' OR lower(file_name) LIKE '%.csv')")
    elif kind == "presentation":
        clauses.append("(lower(file_name) LIKE '%.ppt' OR lower(file_name) LIKE '%.pptx')")
    elif kind == "pdf":
        clauses.append("(mime='application/pdf' OR lower(file_name) LIKE '%.pdf')")
    elif kind == "image":
        clauses.append("mime LIKE 'image/%'")
    elif kind == "video":
        clauses.append("mime LIKE 'video/%'")
    elif kind == "audio":
        clauses.append("mime LIKE 'audio/%'")
    elif kind == "other":
        clauses.append("NOT (mime='application/pdf' OR mime LIKE 'image/%' OR mime LIKE 'video/%' OR mime LIKE 'audio/%' OR lower(file_name) LIKE '%.pdf' OR lower(file_name) LIKE '%.doc' OR lower(file_name) LIKE '%.docx' OR lower(file_name) LIKE '%.txt' OR lower(file_name) LIKE '%.rtf' OR lower(file_name) LIKE '%.xls' OR lower(file_name) LIKE '%.xlsx' OR lower(file_name) LIKE '%.csv' OR lower(file_name) LIKE '%.ppt' OR lower(file_name) LIKE '%.pptx')")
    else:
        return jsonify({"error": "unknown type"}), 404
    q = request.args.get("q", "").strip()
    if q:
        clauses.append("file_name LIKE ?")
        params.append(f"%{q}%")
    page, per_page, offset = _page_params()
    where = " WHERE " + " AND ".join(clauses)
    total = db_scalar("SELECT COUNT(*) FROM files" + where, params) or 0
    rows = db_query("SELECT id, file_name, size, mime, uploaded_at, folder_id, is_favorite FROM files" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [per_page, offset])
    files = []
    for r in rows:
        mime = r["mime"] or ""
        files.append({"id": r["id"], "name": r["file_name"], "size": r["size"],
                      "size_human": human_size(r["size"]), "mime": mime,
                      "uploaded": r["uploaded_at"], "folder_id": r["folder_id"],
                      "is_favorite": bool(r["is_favorite"]),
                      "is_image": mime.startswith("image/"), "is_video": mime.startswith("video/"),
                      "is_audio": mime.startswith("audio/"), "is_pdf": mime == "application/pdf"})
    pages = max(1, (total + per_page - 1) // per_page)
    return jsonify({"kind": kind, "files": files, "total": total, "page": page,
                    "per_page": per_page, "pages": pages})

@app.route("/api/recent")
@login_required
def api_recent():
    limit = max(1, min(5000, int(request.args.get("limit", 5))))
    rows = db_query("SELECT id, file_name, size, mime, uploaded_at, folder_id FROM files ORDER BY id DESC LIMIT ?", (limit,))
    files = []
    for r in rows:
        mime = r["mime"] or ""
        files.append({
            "id": r["id"], "name": r["file_name"], "size": r["size"],
            "size_human": human_size(r["size"]), "mime": mime,
            "uploaded": r["uploaded_at"], "folder_id": r["folder_id"],
            "is_image": mime.startswith("image/"), "is_video": mime.startswith("video/"),
            "is_audio": mime.startswith("audio/"), "is_pdf": mime == "application/pdf",
        })
    return jsonify({"files": files})

@app.route("/api/stats")
@login_required
def api_stats():
    total_files = db_scalar("SELECT COUNT(*) FROM files") or 0
    total_size = db_scalar("SELECT COALESCE(SUM(size),0) FROM files") or 0
    images = db_scalar("SELECT COUNT(*) FROM files WHERE mime LIKE 'image/%'") or 0
    videos = db_scalar("SELECT COUNT(*) FROM files WHERE mime LIKE 'video/%'") or 0
    audios = db_scalar("SELECT COUNT(*) FROM files WHERE mime LIKE 'audio/%'") or 0
    folders = db_scalar("SELECT COUNT(*) FROM folders") or 0
    others = total_files - images - videos - audios
    type_rows = db_query("SELECT lower(file_name) AS name, mime, size FROM files")
    type_sizes = {"document": 0, "spreadsheet": 0, "presentation": 0, "pdf": 0, "image": 0, "video": 0, "audio": 0, "other": 0}
    for r in type_rows:
        name, mime, size = r["name"] or "", r["mime"] or "", r["size"] or 0
        if mime == "application/pdf" or name.endswith(".pdf"): key = "pdf"
        elif mime.startswith("image/"): key = "image"
        elif mime.startswith("video/"): key = "video"
        elif mime.startswith("audio/"): key = "audio"
        elif name.endswith((".xls", ".xlsx", ".csv")): key = "spreadsheet"
        elif name.endswith((".ppt", ".pptx")): key = "presentation"
        elif name.endswith((".doc", ".docx", ".txt", ".rtf")): key = "document"
        else: key = "other"
        type_sizes[key] += size
    return jsonify({
        "total_files": total_files,
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "images": images,
        "videos": videos,
        "audios": audios,
        "others": others,
        "folders": folders,
        "type_sizes": type_sizes,
    })



# ============================================================

@app.route("/api/thumb/<int:fid>")
@login_required
def api_thumb(fid):
    """Generate thumbnail server-side using Pillow"""
    row = db_query("SELECT file_name, msg_id, mime FROM files WHERE id=?", (fid,))
    if not row:
        return "", 404
    r = row[0]
    mime = r["mime"] or ""
    if not mime.startswith("image/"):
        return "", 404
    try:
        msg = run_async(telethon_client.get_messages(CHANNEL, ids=r["msg_id"]))
        if not msg or not msg.media:
            return "", 404
        buf = io.BytesIO()
        run_async(telethon_client.download_media(msg.media, file=buf))
        buf.seek(0)
        from PIL import Image
        img = Image.open(buf)
        img.thumbnail((200, 200), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80)
        out.seek(0)
        return send_file(out, mimetype="image/jpeg")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return "", 404

@app.route("/api/move", methods=["POST"])
@login_required
def api_move():
    data = request.get_json()
    file_ids = data.get("file_ids", [])
    folder_id = int(data.get("folder_id", 0))
    if not file_ids:
        return jsonify({"error": "No files"}), 400
    for fid in file_ids:
        db_exec("UPDATE files SET folder_id=? WHERE id=?", (folder_id, fid))
    return jsonify({"ok": True, "moved": len(file_ids)})
# ============================================================
# GOOGLE OAUTH LOGIN
# ============================================================
@app.route("/auth/google")
def auth_google():
    cid = cfg.get("GDRIVE_CLIENT_ID", "")
    if not cid:
        return "Google OAuth not configured", 500
    scope = "openid email profile"
    params = {
        "client_id": cid,
        "redirect_uri": url_for("drive_ext.gdrive_callback", _external=True, _scheme="https"),
        "response_type": "code",
        "scope": scope,
        "state": "login",
        "access_type": "offline",
        "prompt": "consent",
    }
    session["login_flow"] = True
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return redirect(auth_url)

@app.route("/icons/<path:filename>")
def icon_asset(filename):
    return send_from_directory(ICONS_PATH, filename)

@app.route("/")
def login_page():
    return LOGIN_HTML


@app.route("/drive")
@login_required
def drive_page():
    return DRIVE_HTML

# ============================================================
# LOGIN HTML
# ============================================================
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage — Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d0221;color:#e0e0e0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1a0a3e;border:1px solid #6c3baa;border-radius:16px;padding:48px 40px;width:100%;max-width:420px;box-shadow:0 8px 32px rgba(108,59,170,.3)}
.logo{text-align:center;margin-bottom:32px}
.logo svg{width:64px;height:64px}
.logo h1{font-size:24px;margin-top:12px;color:#a78bfa}
.logo p{color:#8b5cf6;font-size:14px;margin-top:4px}
.form-group{margin-bottom:20px}
.form-group label{display:block;font-size:13px;color:#a78bfa;margin-bottom:6px;font-weight:500}
.form-group input{width:100%;padding:14px 16px;background:#0d0221;border:1px solid #6c3baa;border-radius:10px;color:#e0e0e0;font-size:16px;outline:none;transition:.2s}
.form-group input:focus{border-color:#a78bfa;box-shadow:0 0 0 3px rgba(167,139,250,.2)}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#6c3baa,#a78bfa);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:.2s}
.btn:hover{opacity:.9}
.error{color:#f87171;font-size:13px;margin-top:8px;display:none}
.hint{color:#6b7280;font-size:12px;margin-top:16px;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
    <h1>Cloud Storage</h1>
    <p>Telegram-powered file storage</p>
  </div>
  <div class="form-group">
    <label>Telegram User ID</label>
    <input type="text" id="uid" placeholder="Masukkan ID anda (angka)" inputmode="numeric">
  </div>
  <div style="margin:20px 0;position:relative;text-align:center">
    <div style="position:absolute;top:50%;left:0;right:0;height:1px;background:#6c3baa"></div>
    <span style="position:relative;background:#1a0a3e;padding:0 16px;font-size:13px;color:#6b7280">atau</span>
  </div>
  <a href="/auth/google" class="btn-google">
    <svg width="20" height="20" viewBox="0 0 24 24"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>
    Masuk dengan Google
  </a>
  <style>.btn-google{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:14px;background:#fff;color:#333;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;text-decoration:none;transition:.2s;margin-bottom:16px}.btn-google:hover{opacity:.9}</style>
  <button class="btn" id="loginBtn" onclick="doLogin()">Masuk dengan ID</button>
  <div class="error" id="err"></div>
  <div class="hint">Hanya user terdaftar yang bisa mengakses</div>
</div>
<script>
document.getElementById('uid').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
async function doLogin(){
  const uid=document.getElementById('uid').value.trim();
  const err=document.getElementById('err');
  if(!uid){err.textContent='ID wajib diisi';err.style.display='block';return;}
  err.style.display='none';
  document.getElementById('loginBtn').disabled=true;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid})});
  const d=await r.json();
  if(d.ok) location.href='/drive';
  else{err.textContent=d.error;err.style.display='block';document.getElementById('loginBtn').disabled=false;}
}
</script>
</body>
</html>'''

# ============================================================
# DRIVE HTML (with folders + progress + no refresh)
# ============================================================
DRIVE_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0a1a;--bg2:#12122a;--bg3:#1a1a3a;--border:#2a2a4a;--text:#e0e0f0;--muted:#9292ac;--accent:#8b5cf6;--accent2:#a78bfa;--green:#22c55e;--red:#ef4444;--yellow:#eab308;--pink:#ec4899;--cyan:#06b6d4}
html[data-theme="light"]{--bg:#f3f5fb;--bg2:#ffffff;--bg3:#eef1f8;--border:#d6dbea;--text:#172036;--muted:#667085;--accent:#7c3aed;--accent2:#6d28d9;--green:#16a34a;--red:#dc2626;--yellow:#ca8a04;--pink:#db2777;--cyan:#0891b2;color-scheme:light}
html[data-theme="dark"]{color-scheme:dark}
body,.sidebar,.right-sidebar,.right-card,.stat,.drop-zone,.folder-card,.files-panel,.topbar .btn,.search-box input,.storage-card-side{transition:background-color .25s,border-color .25s,color .25s}
html[data-theme="light"] .nav-item.active{color:#fff}
html[data-theme="light"] .search-box::before{filter:brightness(.35)}
html[data-theme="light"] .cs-icon{filter:saturate(1.1)}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh}
a{color:var(--accent2);text-decoration:none}
/* ===== SIDEBAR ===== */
.sidebar{width:240px;background:var(--bg2);border-right:1px solid var(--border);padding:16px 0;position:fixed;top:0;bottom:0;overflow-y:auto;display:flex;flex-direction:column;z-index:50}
.sidebar-logo{display:flex;align-items:center;gap:10px;padding:8px 20px 20px;font-size:1.1rem;font-weight:700;color:var(--text)}
.sidebar-logo span{font-size:1.4rem}
.cs-icon{width:20px;height:20px;display:inline-block;vertical-align:middle;object-fit:contain;flex-shrink:0}
.cs-icon.sm{width:16px;height:16px}.cs-icon.md{width:24px;height:24px}.cs-icon.lg{width:38px;height:38px}
.cs-file-icon{width:32px;height:40px;display:inline-block;vertical-align:middle;object-fit:contain;flex-shrink:0}
.icon-label{display:inline-flex;align-items:center;gap:7px}
.nav-section{padding:0 12px;margin-bottom:8px}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;color:var(--muted);cursor:pointer;transition:all .2s;font-size:.9rem}
.nav-item:hover,.nav-item.active{background:rgba(139,92,246,.15);color:var(--accent2)}
.nav-item.active{background:var(--accent);color:#fff;font-weight:600}
.nav-item span{font-size:1.1rem;width:24px;text-align:center}
.nav-divider{height:1px;background:var(--border);margin:12px 20px}
.storage-card-side{margin:0 16px;padding:16px;background:var(--bg3);border-radius:12px;border:1px solid var(--border);text-align:center}
.storage-card-side .infinity{font-size:2rem;color:var(--accent)}
.storage-card-side .label{font-size:.8rem;color:var(--muted);margin-top:4px}
.storage-card-side .sublabel{font-size:.7rem;color:var(--muted);margin-top:2px}
.dark-toggle{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;margin-top:auto}
.dark-toggle span{font-size:.85rem;color:var(--muted)}
.toggle{width:40px;height:22px;background:var(--accent);border-radius:11px;position:relative;cursor:pointer;border:0;padding:0;flex-shrink:0;box-shadow:inset 0 0 0 1px rgba(255,255,255,.12)}
.toggle .knob{width:18px;height:18px;background:#fff;border-radius:50%;position:absolute;top:2px;right:2px;transition:all .2s;box-shadow:0 1px 4px rgba(0,0,0,.35)}
html[data-theme="light"] .toggle{background:#c7ccda}
html[data-theme="light"] .toggle .knob{right:20px}
/* ===== MAIN ===== */
.main-wrap{margin-left:240px;flex:1;margin-right:280px;padding:16px 24px;min-height:100vh}
/* ===== TOP BAR ===== */
.topbar{display:flex;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.search-box{flex:1;min-width:200px;position:relative}
.search-box input{width:100%;padding:10px 16px 10px 40px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:.9rem}
.search-box input:focus{outline:none;border-color:var(--accent)}
.search-box::before{content:"";position:absolute;left:12px;top:50%;transform:translateY(-50%);width:18px;height:18px;background:url('/icons/ui/search.svg') center/contain no-repeat}
.search-box .shortcut{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:.65rem;color:var(--muted)}
.user-id{color:var(--muted);font-size:.85rem;white-space:nowrap}
.topbar .btn{padding:7px 14px;border:1px solid var(--border);background:var(--bg2);color:var(--text);border-radius:8px;cursor:pointer;font-size:.82rem;transition:all .2s;white-space:nowrap}
.topbar .btn:hover{border-color:var(--accent);background:var(--bg3)}
.topbar .btn.gdrive{border-color:var(--green);color:var(--green)}
.topbar .btn.gphotos{border-color:var(--pink);color:var(--pink)}
.topbar .btn.danger{border-color:var(--red);color:var(--red)}
/* ===== STATS ===== */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px}
.stat{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;display:flex;align-items:center;gap:12px}
.stat .icon{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.4rem;flex-shrink:0}
.stat .icon.purple{background:rgba(139,92,246,.2);color:var(--accent)}
.stat .icon.cyan{background:rgba(6,182,212,.2);color:var(--cyan)}
.stat .icon.yellow{background:rgba(234,179,8,.2);color:var(--yellow)}
.stat .icon.green{background:rgba(34,197,94,.2);color:var(--green)}
.stat .icon.pink{background:rgba(236,72,153,.2);color:var(--pink)}
.stat .num{font-size:1.3rem;font-weight:700}
.stat .lbl{font-size:.75rem;color:var(--muted)}
.stat[role="button"]{cursor:pointer;transition:all .2s}
.stat[role="button"]:hover{border-color:var(--accent2);transform:translateY(-1px)}
.stat.active{border-color:var(--accent);background:rgba(139,92,246,.2);box-shadow:0 0 0 2px rgba(139,92,246,.18)}
.type-item{cursor:pointer;border:1px solid transparent;border-radius:7px;padding:2px 5px;transition:all .2s}
.type-item.active{border-color:var(--accent);background:rgba(139,92,246,.18);color:var(--accent2)}
.btn.active,.toolbar .btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.sort-select.active{border-color:var(--accent);box-shadow:0 0 0 2px rgba(139,92,246,.15)}
/* ===== TOOLBAR ===== */
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.toolbar .btn{padding:8px 14px;border:1px solid var(--border);background:var(--bg2);color:var(--text);border-radius:8px;cursor:pointer;font-size:.85rem;transition:all .2s;display:flex;align-items:center;gap:6px}
.toolbar .btn:hover{border-color:var(--accent)}
.toolbar .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.toolbar .btn.primary:hover{background:var(--accent2)}
.toolbar-right{margin-left:auto;display:flex;gap:6px;align-items:center}
.view-btn{padding:6px 10px;border:1px solid var(--border);background:var(--bg2);color:var(--muted);border-radius:6px;cursor:pointer;font-size:.85rem}
.view-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.sort-select{padding:6px 10px;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:.85rem}
/* ===== DROP ZONE ===== */
.drop-zone{border:2px dashed var(--border);border-radius:16px;padding:40px 20px;text-align:center;margin-bottom:20px;transition:all .2s;cursor:pointer;background:var(--bg2)}
.drop-zone:hover,.drop-zone.dragover{border-color:var(--accent);background:rgba(139,92,246,.05)}
.drop-zone .dz-icon{font-size:2.5rem;margin-bottom:8px}
.drop-zone .dz-title{font-size:1rem;color:var(--text);margin-bottom:4px}
.drop-zone .dz-sub{font-size:.8rem;color:var(--muted);margin-bottom:12px}
.drop-zone .pick-btn{display:inline-block;padding:8px 20px;background:var(--accent);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:.85rem}
.file-types{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:14px}
.file-types .ft{padding:4px 10px;border-radius:6px;font-size:.7rem;font-weight:600;border:1px solid}
.ft.pdf{background:rgba(239,68,68,.15);color:var(--red);border-color:rgba(239,68,68,.3)}
.ft.doc{background:rgba(59,130,246,.15);color:#3b82f6;border-color:rgba(59,130,246,.3)}
.ft.xls{background:rgba(34,197,94,.15);color:var(--green);border-color:rgba(34,197,94,.3)}
.ft.ppt{background:rgba(249,115,22,.15);color:#f97316;border-color:rgba(249,115,22,.3)}
.ft.txt{background:rgba(136,136,136,.15);color:var(--muted);border-color:rgba(136,136,136,.3)}
.ft.zip{background:rgba(234,179,8,.15);color:var(--yellow);border-color:rgba(234,179,8,.3)}
.ft.img{background:rgba(34,197,94,.15);color:var(--green);border-color:rgba(34,197,94,.3)}
.ft.vid{background:rgba(236,72,153,.15);color:var(--pink);border-color:rgba(236,72,153,.3)}
.ft.audio{background:rgba(139,92,246,.15);color:var(--accent);border-color:rgba(139,92,246,.3)}
.ft.more{background:rgba(136,136,136,.1);color:var(--muted);border-color:rgba(136,136,136,.2)}
/* ===== FOLDERS ===== */
.folder-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.folder-heading .section-title{margin-bottom:0}
.folder-toggle{border:1px solid rgba(139,92,246,.45);background:rgba(139,92,246,.08);color:var(--accent2);border-radius:7px;padding:5px 10px;font-size:.72rem;font-weight:600;cursor:pointer}
.folder-toggle:hover{background:rgba(139,92,246,.18);border-color:var(--accent)}
.section-title{font-size:1rem;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.folder-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.folder-card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;display:flex;align-items:center;gap:12px;cursor:pointer;transition:all .2s;position:relative}
.folder-card:hover{border-color:var(--accent);background:var(--bg3)}
.folder-card .fc-icon{font-size:2rem;flex-shrink:0}
.folder-card .fc-info{flex:1;min-width:0}
.folder-card .fc-name{font-size:.85rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.folder-card .fc-count{font-size:.7rem;color:var(--muted);margin-top:2px}
.folder-card .fc-menu{color:var(--muted);cursor:pointer;font-size:1.2rem;padding:4px}
/* ===== FILES TABLE ===== */
.files-panel{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:20px}
.files-panel .panel-title{font-size:1rem;font-weight:600;margin-bottom:12px}
.files-table{width:100%;border-collapse:collapse}
.files-table th{text-align:left;padding:8px 12px;font-size:.75rem;color:var(--muted);border-bottom:1px solid var(--border);font-weight:500}
.files-table td{padding:10px 12px;border-bottom:1px solid rgba(42,42,74,.5);font-size:.85rem;vertical-align:middle}
.files-table tr:hover{background:rgba(139,92,246,.05)}
.files-table .fname-cell{display:flex;align-items:center;gap:10px}
.files-table .fname-cell img{width:36px;height:36px;border-radius:6px;object-fit:cover;background:var(--bg3)}
.files-table .fname-cell .ficon{width:36px;height:36px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:1.2rem;background:var(--bg3);flex-shrink:0}
.files-table .owner-badge{background:var(--bg3);padding:3px 10px;border-radius:12px;font-size:.75rem;color:var(--muted)}
.files-table .act-btn{padding:4px 8px;border:none;background:none;color:var(--muted);cursor:pointer;font-size:1rem;border-radius:4px}
.files-table .act-btn:hover{color:var(--accent);background:rgba(139,92,246,.1)}
.files-table .fav-btn{font-size:1.15rem;color:var(--muted)}
.files-table .fav-btn.active{color:#fbbf24;background:rgba(251,191,36,.12)}
.del-btn{display:none;border:none;background:none;color:var(--red);cursor:pointer;font-size:.85rem;padding:4px;border-radius:4px}
.folder-card:hover .del-btn,.files-table tr:hover .del-btn{display:inline-block}
/* ===== RIGHT SIDEBAR ===== */
.right-sidebar{width:280px;position:fixed;top:0;right:0;bottom:0;background:var(--bg2);border-left:1px solid var(--border);padding:16px;overflow-y:auto}
.right-card{background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.right-card .rc-title{font-size:.9rem;font-weight:600;margin-bottom:12px}
.donut-wrap{text-align:center;padding:10px 0}
.donut{width:120px;height:120px;border-radius:50%;position:relative;margin:0 auto}
.donut-center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}
.donut-center .d-icon{font-size:1.5rem}
.donut-center .d-label{font-size:.65rem;color:var(--muted)}
.donut-legend{display:flex;flex-direction:column;gap:6px;margin-top:12px}
.donut-legend .dl{display:flex;align-items:center;gap:8px;font-size:.8rem}
.donut-legend .dl .dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.storage-btn{width:100%;padding:10px;background:none;border:1px solid var(--accent);color:var(--accent);border-radius:8px;cursor:pointer;font-size:.85rem;margin-top:12px;transition:all .2s}
.storage-btn:hover{background:var(--accent);color:#fff}
.type-list{display:flex;flex-direction:column;gap:8px}
.type-item{display:flex;align-items:center;gap:10px;font-size:.8rem}
.type-item .ti-icon{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:.9rem;flex-shrink:0}
.type-item .ti-name{flex:1;color:var(--muted)}
.type-item .ti-size{font-weight:500}
.see-all{display:block;text-align:center;margin-top:10px;font-size:.8rem;color:var(--accent2)}
.recent-mini{display:flex;flex-direction:column;gap:10px}
.recent-mini .rm{display:flex;align-items:center;gap:10px}
.recent-mini .rm .rm-icon{width:32px;height:32px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:.9rem;flex-shrink:0}
.recent-mini .rm .rm-info{flex:1;min-width:0}
.recent-mini .rm .rm-name{font-size:.8rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.recent-mini .rm .rm-date{font-size:.7rem;color:var(--muted)}
/* ===== BREADCRUMB ===== */
.breadcrumb{font-size:.85rem;color:var(--muted);margin-bottom:12px}
.breadcrumb a{color:var(--accent2);cursor:pointer}
.breadcrumb span{margin:0 4px}
/* ===== PAGINATION ===== */
.pagination{display:flex;justify-content:center;gap:6px;margin-top:16px}
.pagination .btn{min-width:36px;text-align:center;padding:6px 10px;border:1px solid var(--border);background:var(--bg2);color:var(--text);border-radius:6px;cursor:pointer;font-size:.85rem}
.pagination .btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
/* ===== MODALS ===== */
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:20px;width:90%;max-width:400px}
.modal h3{color:var(--accent2);margin-bottom:12px}
.modal input[type=text]{width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);margin-bottom:12px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.modal-actions .btn{padding:8px 16px;border:1px solid var(--border);background:var(--bg3);color:var(--text);border-radius:6px;cursor:pointer}
.modal-actions .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.selected-bar{display:none;background:var(--bg3);border:1px solid var(--accent);border-radius:8px;padding:10px 16px;margin-bottom:12px;align-items:center;gap:10px}
.selected-bar.show{display:flex}
#filePreview{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.85);z-index:300;display:none;align-items:center;justify-content:center;flex-direction:column}
#filePreview.show{display:flex}
#filePreview img,#filePreview video{max-width:90%;max-height:80vh;border-radius:8px}
#filePreview .close-btn{position:absolute;top:16px;right:20px;font-size:2rem;color:#fff;cursor:pointer}
.empty{text-align:center;color:var(--muted);padding:40px 20px}
#uploadProgress{margin-top:10px;display:none}
.progress-bar{height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-top:6px}
.progress-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .3s}
/* ===== STORAGE CARD POLISH ===== */
.storage-card-main{background:linear-gradient(155deg,rgba(139,92,246,.08),transparent 44%),var(--bg3);box-shadow:0 10px 28px rgba(0,0,0,.18),inset 0 1px rgba(255,255,255,.025)}
.storage-card-main .rc-title{display:flex;align-items:center;gap:7px}
.storage-card-main .donut-wrap{display:grid;grid-template-columns:92px 1fr;align-items:center;gap:8px;text-align:left}
.storage-card-main .donut{margin:0;width:88px;height:88px;padding:9px;background:var(--bg)!important}
.storage-card-main .donut>div:first-child{box-shadow:inset 0 0 12px rgba(0,0,0,.2)}
.storage-card-main .donut::after{content:"";position:absolute;inset:17px;border-radius:50%;background:var(--bg3);z-index:1}
.storage-card-main .donut-center{z-index:2;width:58px}
.storage-card-main .donut-center .d-icon .cs-icon{width:22px;height:22px}
.storage-card-main .donut-center .d-label{line-height:1.15;margin-top:1px}
.storage-card-main .donut-legend{margin:0;gap:9px}
.storage-card-main .donut-legend .dl{display:grid;grid-template-columns:8px 1fr;gap:2px 7px;align-items:center}
.storage-card-main .donut-legend .dl strong{grid-column:2;font-size:.7rem;color:var(--text)}
.storage-card-main .donut-legend .dot{width:8px!important;height:8px!important}
.storage-card-main .storage-btn{display:flex;align-items:center;justify-content:center;gap:6px;font-weight:600}
html[data-theme="light"] .storage-card-main{box-shadow:0 10px 25px rgba(31,41,55,.08)}
/* ===== REFERENCE MATCH: dense 1280x720 dashboard ===== */
@media(min-width:1101px){
 body{height:100vh;overflow:hidden}
 .sidebar{width:190px;padding:10px 0}.sidebar-logo{padding:5px 15px 12px;font-size:.93rem}.nav-section{padding:0 9px}.nav-item{padding:7px 9px;font-size:.76rem;gap:7px}.nav-divider{margin:7px 15px}.storage-card-side{margin:0 12px;padding:9px}.storage-card-side .infinity{line-height:1}.storage-card-side .label{font-size:.72rem}.storage-card-side .sublabel{font-size:.62rem}.dark-toggle{padding:7px 15px}.dark-toggle span{font-size:.72rem}
 .main-wrap{margin-left:190px;margin-right:235px;padding:10px 14px;height:100vh;overflow:auto}.right-sidebar{width:235px;padding:10px}
 .topbar{gap:6px;margin-bottom:9px;flex-wrap:nowrap;height:34px}.search-box{min-width:160px}.search-box input{height:34px;padding-top:7px;padding-bottom:7px;font-size:.75rem}.search-box .shortcut{font-size:.55rem}.user-id{font-size:.67rem;max-width:125px;overflow:hidden;text-overflow:ellipsis}.topbar .btn{padding:5px 7px;font-size:.65rem;display:inline-flex;align-items:center;gap:4px}
 .stats{gap:7px;margin-bottom:9px}.stat{padding:8px;gap:7px;border-radius:9px;min-width:0}.stat .icon{width:32px;height:32px}.stat .num{font-size:.95rem;white-space:nowrap}.stat .lbl{font-size:.62rem}
 .breadcrumb{display:none}.toolbar{gap:6px;margin-bottom:7px}.toolbar .btn{padding:6px 9px;font-size:.7rem}.view-btn,.sort-select{padding:5px 7px;font-size:.7rem}
 .drop-zone{height:145px;padding:8px 14px;margin-bottom:9px;border-radius:11px}.drop-zone .dz-icon{font-size:0;margin-bottom:0}.drop-zone .dz-icon .cs-icon{width:30px;height:30px}.drop-zone .dz-title{font-size:.76rem;margin-bottom:0}.drop-zone .dz-sub{font-size:.62rem;margin-bottom:3px}.drop-zone .pick-btn{padding:4px 14px;font-size:.67rem}.file-types{gap:8px;margin-top:6px;align-items:flex-end}.file-types .ft{padding:0;border:0;background:none;display:flex;flex-direction:column;align-items:center;gap:0;font-size:.5rem}.file-types .ft img{width:20px;height:25px}
 .folder-heading{margin-bottom:6px}.folder-heading .section-title{font-size:.78rem}.folder-toggle{padding:4px 8px;font-size:.61rem}.folder-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-bottom:9px}.folder-card{padding:8px;gap:7px;border-radius:8px;height:50px}.folder-card .fc-icon .cs-icon{width:28px;height:28px}.folder-card .fc-name{font-size:.67rem}.folder-card .fc-count{font-size:.56rem}.folder-grid:not(.show-all) .folder-card:nth-child(n+5){display:none}
 .files-panel{padding:9px;border-radius:9px;margin-bottom:0}.files-panel .panel-title{font-size:.77rem;margin-bottom:5px}.files-table th{padding:4px 7px;font-size:.57rem}.files-table td{padding:4px 7px;font-size:.61rem;height:38px}.files-table .fname-cell{gap:6px}.files-table .fname-cell>img,.files-table .fname-cell .ficon{width:26px;height:28px}.files-table .fname-cell .ficon .cs-file-icon{width:23px;height:28px}.files-table .owner-badge{padding:2px 7px;font-size:.56rem}.pagination{display:flex}
 .right-card{padding:10px;margin-bottom:8px;border-radius:9px}.right-card .rc-title{font-size:.73rem;margin-bottom:6px}.donut-wrap{padding:2px 0}.donut-center .d-icon{font-size:1rem}.donut-center .d-label{font-size:.55rem}.donut-legend{gap:3px;margin-top:5px}.donut-legend .dl{font-size:.61rem}.storage-btn{padding:6px;font-size:.61rem;margin-top:6px}.type-list{gap:3px}.type-item{gap:6px;font-size:.6rem}.type-item .ti-icon{width:23px;height:23px}.type-item .ti-icon .cs-icon{width:18px;height:18px}.see-all{margin-top:5px;font-size:.6rem}.recent-mini{gap:5px}.recent-mini .rm{gap:6px}.recent-mini .rm .rm-icon{width:25px;height:27px}.recent-mini .rm .rm-icon .cs-file-icon{width:22px;height:27px}.recent-mini .rm .rm-name{font-size:.59rem}.recent-mini .rm .rm-date{font-size:.5rem}
}
@media(max-width:1100px){.right-sidebar{display:none}.main-wrap{margin-right:0}}
@media(max-width:768px){.sidebar{display:none}.main-wrap{margin-left:0}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<!-- SIDEBAR -->
<div class="sidebar">
  <div class="sidebar-logo"><img class="cs-icon md" src="/icons/ui/cloud-storage.svg" alt=""> Cloud Storage</div>
  <div class="nav-section">
    <div class="nav-item active" data-nav="dashboard" onclick="openNav('dashboard',this)"><img class="cs-icon" src="/icons/ui/dashboard.svg" alt=""> Dashboard</div>
    <div class="nav-item" data-nav="recent" onclick="openNav('recent',this)"><img class="cs-icon" src="/icons/ui/recent.svg" alt=""> Recent</div>
    <div class="nav-item" data-nav="favorites" onclick="openNav('favorites',this)"><img class="cs-icon" src="/icons/ui/favorites.svg" alt=""> Favorites</div>
    <div class="nav-item" data-nav="shared" onclick="openNav('shared',this)"><img class="cs-icon" src="/icons/ui/shared-with-me.svg" alt=""> Shared with me</div>
    <div class="nav-item" data-nav="trash" onclick="openNav('trash',this)"><img class="cs-icon" src="/icons/ui/trash.svg" alt=""> Trash</div>
  </div>
  <div class="nav-divider"></div>
  <div class="storage-card-side">
    <div class="infinity"><img class="cs-icon lg" src="/icons/ui/unlimited.svg" alt=""></div>
    <div class="label">Unlimited Storage</div>
    <div class="sublabel">Penyimpanan Tanpa Batas</div>
  </div>
  <div class="dark-toggle">
    <span class="icon-label"><img class="cs-icon" src="/icons/ui/dark-mode.svg" alt=""> <span id="themeLabel">Mode Gelap</span></span>
    <button class="toggle" id="themeToggle" type="button" role="switch" aria-checked="true" aria-label="Ubah tema" onclick="toggleTheme()"><span class="knob"></span></button>
  </div>
</div>

<!-- MAIN CONTENT -->
<div class="main-wrap">
  <!-- TOP BAR -->
  <div class="topbar">
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="Cari file atau folder..." oninput="debounceSearch()">
      <span class="shortcut">Ctrl /</span>
    </div>
    <span class="user-id" id="userId"></span>
    <button class="btn" onclick="location.href='/profile'"><img class="cs-icon sm" src="/icons/ui/profile.svg" alt=""> Profile</button>
    <button class="btn gdrive" onclick="location.href='/gdrive'"><img class="cs-icon sm" src="/icons/ui/google-drive.svg" alt=""> Google Drive</button>
    <button class="btn gphotos" onclick="location.href='/photos'"><img class="cs-icon sm" src="/icons/ui/google-photos.svg" alt=""> Google Photos</button>
    <button class="btn danger" onclick="doLogout()"><img class="cs-icon sm" src="/icons/ui/logout.svg" alt=""> Logout</button>
  </div>

  <!-- STATS -->
  <div class="stats" id="stats"></div>

  <!-- BREADCRUMB -->
  <div class="breadcrumb" id="breadcrumb"><a onclick="goFolder(0)">Home</a></div>

  <!-- TOOLBAR -->
  <div class="toolbar">
    <button class="btn primary" onclick="showNewFolderModal()"><img class="cs-icon sm" src="/icons/ui/plus.svg" alt=""> New Folder</button>
    <button class="btn" onclick="triggerUpload()"><img class="cs-icon sm" src="/icons/ui/upload.svg" alt=""> Upload</button>
    <button class="btn" id="selectBtn" onclick="toggleSelectMode()"><img class="cs-icon sm" src="/icons/ui/select.svg" alt=""> Select</button>
    <div class="toolbar-right">
      <button class="view-btn active" onclick="setViewMode('grid',this)"><img class="cs-icon sm" src="/icons/ui/grid.svg" alt="Grid"></button>
      <button class="view-btn" onclick="setViewMode('list',this)"><img class="cs-icon sm" src="/icons/ui/list.svg" alt="List"></button>
      <select class="sort-select" id="pageSize" title="Jumlah per halaman" onchange="changePageSize()">
        <option value="10">10 / halaman</option>
        <option value="25" selected>25 / halaman</option>
        <option value="50">50 / halaman</option>
        <option value="100">100 / halaman</option>
      </select>
      <select class="sort-select" id="sortSelect" onchange="this.classList.add('active');loadCurrentView()">
        <option>Terbaru</option>
        <option>Nama</option>
        <option>Ukuran</option>
      </select>
    </div>
  </div>

  <!-- DROP ZONE -->
  <div class="drop-zone" id="dropZone">
    <div class="dz-icon"><img class="cs-icon lg" src="/icons/ui/cloud-upload.svg" alt=""></div>
    <div class="dz-title">Seret file ke sini atau klik untuk upload</div>
    <div class="dz-sub">Mendukung semua jenis file</div>
    <button class="pick-btn" onclick="triggerUpload()">Pilih File</button>
    <div class="file-types">
      <span class="ft pdf"><img src="/icons/file-types/pdf.svg" alt="">PDF</span><span class="ft doc"><img src="/icons/file-types/docx.svg" alt="">DOCX</span><span class="ft xls"><img src="/icons/file-types/xlsx.svg" alt="">XLSX</span>
      <span class="ft ppt"><img src="/icons/file-types/pptx.svg" alt="">PPTX</span><span class="ft txt"><img src="/icons/file-types/txt.svg" alt="">TXT</span><span class="ft zip"><img src="/icons/file-types/zip.svg" alt="">ZIP</span>
      <span class="ft zip"><img src="/icons/file-types/rar.svg" alt="">RAR</span><span class="ft img"><img src="/icons/file-types/jpg.svg" alt="">JPG</span><span class="ft img"><img src="/icons/file-types/png.svg" alt="">PNG</span>
      <span class="ft vid"><img src="/icons/file-types/mp4.svg" alt="">MP4</span><span class="ft audio"><img src="/icons/file-types/mp3.svg" alt="">MP3</span><span class="ft more"><img src="/icons/file-types/other.svg" alt="">6 Lainnya</span>
    </div>
    <input type="file" id="fileInput" multiple style="display:none">
  </div>

  <!-- SELECTED BAR -->
  <div class="selected-bar" id="selectedBar">
    <span id="selectedCount">0 dipilih</span>
    <button class="btn" onclick="moveSelected()">&#128193; Pindah</button>
    <button class="btn" style="border-color:var(--red);color:var(--red)" onclick="deleteSelected()">&#128465; Hapus</button>
    <button class="btn" onclick="toggleSelectMode()">Batal</button>
  </div>

  <!-- UPLOAD PROGRESS -->
  <div id="uploadProgress">
    <div id="uploadStatus" style="font-size:.85rem;color:var(--muted)"></div>
    <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  </div>

  <!-- FOLDERS -->
  <div class="folder-heading">
    <div class="section-title" id="folderTitle"><img class="cs-icon" src="/icons/ui/folder.svg" alt=""> Folder Saya</div>
    <button class="folder-toggle" id="folderToggle" type="button" onclick="toggleAllFolders()" style="display:none">Lihat Semua</button>
  </div>
  <div class="folder-grid" id="foldersGrid"></div>

  <!-- FILES TABLE -->
  <div class="files-panel">
    <div class="panel-title icon-label" id="fileTitle"><img class="cs-icon" src="/icons/ui/file.svg" alt=""> File Terbaru</div>
    <table class="files-table" id="filesTable">
      <thead><tr><th>Nama</th><th>Owner</th><th>Ukuran</th><th>Diubah</th><th>Aksi</th></tr></thead>
      <tbody id="filesBody"></tbody>
    </table>
    <div id="filesEmpty" class="empty" style="display:none">Tidak ada file</div>
    <div class="pagination" id="pagination"></div>
  </div>
</div>

<!-- RIGHT SIDEBAR -->
<div class="right-sidebar">
  <!-- STORAGE -->
  <div class="right-card storage-card-main">
    <div class="rc-title icon-label"><img class="cs-icon" src="/icons/ui/storage.svg" alt=""> Penyimpanan</div>
    <div class="donut-wrap">
      <div class="donut" id="donutChart"></div>
      <div class="donut-legend">
        <div class="dl"><span class="dot" style="background:var(--accent)"></span> <span>Terpakai</span> <strong id="usedSize">-</strong></div>
        <div class="dl"><span class="dot" style="background:var(--bg)"></span> <span>Kapasitas</span> <strong>Tanpa Batas</strong></div>
      </div>
    </div>
    <button class="storage-btn"><img class="cs-icon sm" src="/icons/ui/unlimited.svg" alt=""> Penyimpanan Tak Terbatas</button>
  </div>
  <!-- FILE TYPES -->
  <div class="right-card">
    <div class="rc-title icon-label"><img class="cs-icon" src="/icons/ui/file.svg" alt=""> Tipe File</div>
    <div class="type-list" id="typeList"></div>
    <a class="see-all" href="#" onclick="showAllFileTypes(event)">Lihat Semua &rarr;</a>
  </div>
  <!-- RECENT MINI -->
  <div class="right-card">
    <div class="rc-title icon-label"><img class="cs-icon" src="/icons/ui/recent.svg" alt=""> File Terbaru</div>
    <div class="recent-mini" id="recentMini"></div>
    <a class="see-all" href="#" onclick="showAllRecent(event)">Lihat semua &rarr;</a>
  </div>
</div>

<!-- MODALS -->
<div class="modal-overlay" id="newFolderModal">
  <div class="modal">
    <h3 class="icon-label"><img class="cs-icon" src="/icons/ui/folder.svg" alt=""> Folder Baru</h3>
    <input type="text" id="newFolderName" placeholder="Nama folder..." onkeydown="if(event.key==='Enter')createFolder()">
    <div class="modal-actions">
      <button class="btn" onclick="closeModal('newFolderModal')">Batal</button>
      <button class="btn primary" onclick="createFolder()">Buat</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="moveModal">
  <div class="modal">
    <h3 class="icon-label"><img class="cs-icon" src="/icons/ui/folder.svg" alt=""> Pindah ke Folder</h3>
    <div id="moveFolderList" style="max-height:300px;overflow-y:auto;margin-bottom:12px"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal('moveModal')">Batal</button>
    </div>
  </div>
</div>
<div id="filePreview" onclick="if(event.target===this||event.target.classList.contains('close-btn'))this.classList.remove('show')">
  <span class="close-btn">&times;</span>
  <div id="previewContent"></div>
</div>
<div id="err"></div>

<script>
function applyTheme(theme){
  var value=theme==='light'?'light':'dark';
  document.documentElement.setAttribute('data-theme',value);
  var btn=document.getElementById('themeToggle'),label=document.getElementById('themeLabel');
  if(btn)btn.setAttribute('aria-checked',value==='dark'?'true':'false');
  if(label)label.textContent=value==='dark'?'Mode Gelap':'Mode Terang';
}
function toggleTheme(){
  var next=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  try{localStorage.setItem('cloud-theme',next);}catch(e){}
  applyTheme(next);
}
(function(){var saved='dark';try{saved=localStorage.getItem('cloud-theme')||'dark';}catch(e){}applyTheme(saved);})();
var currentPage=1,perPage=25,currentFolder=0,currentView='dashboard',currentType='',selectMode=false,selectedIds=new Set(),searchTimer=null;

function api(url,opts){
  return fetch(url,Object.assign({credentials:'same-origin'},opts||{}))
    .then(function(r){if(!r.ok)return{};return r.json();})
    .catch(function(e){return{};});
}
function doLogout(){api('/api/logout',{method:'POST'}).then(function(){location.href='/';});}
function init(){
  api('/api/me').then(function(me){
    if(!me||!me.logged_in){location.href='/';return;}
    document.getElementById('userId').textContent=me.google_email||('ID: '+me.user_id);
    loadAll();
    setupDragDrop();
  });
}
function loadAll(){loadStats();loadFolders();loadRecentDashboard();}

function loadStats(){
  api('/api/stats').then(function(s){
    if(!s||s.total_files===undefined)return;
    document.getElementById('stats').innerHTML=
      '<div class="stat" role="button" tabindex="0" data-view="files" onclick="openAllFiles()" onkeydown="if(event.key===&quot;Enter&quot;)openAllFiles()"><div class="icon purple"><img class="cs-icon md" src="/icons/ui/file.svg" alt=""></div><div><div class="num">'+s.total_files+'</div><div class="lbl">File</div></div></div>'+
      '<div class="stat" role="button" tabindex="0" data-view="size" onclick="openStorageFiles()" onkeydown="if(event.key===&quot;Enter&quot;)openStorageFiles()"><div class="icon cyan"><img class="cs-icon md" src="/icons/ui/size-chart.svg" alt=""></div><div><div class="num">'+s.total_size_human+'</div><div class="lbl">Ukuran</div></div></div>'+
      '<div class="stat" role="button" tabindex="0" data-view="folders" onclick="openFolderOverview()" onkeydown="if(event.key===&quot;Enter&quot;)openFolderOverview()"><div class="icon yellow"><img class="cs-icon md" src="/icons/ui/folder.svg" alt=""></div><div><div class="num">'+s.folders+'</div><div class="lbl">Folder</div></div></div>'+
      '<div class="stat" role="button" tabindex="0" data-view="image" onclick="openFileType(&quot;image&quot;)" onkeydown="if(event.key===&quot;Enter&quot;)openFileType(&quot;image&quot;)"><div class="icon green"><img class="cs-icon md" src="/icons/ui/photo.svg" alt=""></div><div><div class="num">'+s.images+'</div><div class="lbl">Foto</div></div></div>'+
      '<div class="stat" role="button" tabindex="0" data-view="video" onclick="openFileType(&quot;video&quot;)" onkeydown="if(event.key===&quot;Enter&quot;)openFileType(&quot;video&quot;)"><div class="icon pink"><img class="cs-icon md" src="/icons/ui/video.svg" alt=""></div><div><div class="num">'+s.videos+'</div><div class="lbl">Video</div></div></div>';
    document.getElementById('usedSize').textContent=s.total_size_human;
    document.getElementById('donutChart').innerHTML='<div style="width:100%;height:100%;border-radius:50%;background:conic-gradient(var(--accent) 0% 24%,var(--bg) 24% 100%)"></div><div class="donut-center"><div class="d-icon"><img class="cs-icon md" src="/icons/ui/unlimited.svg" alt=""></div><div class="d-label">Tak<br>terbatas</div></div>';
    var z=s.type_sizes||{};
    var types=[
      ['Dokumen','document','doc.svg'],['Spreadsheet','spreadsheet','xlsx.svg'],['Presentasi','presentation','pptx.svg'],['PDF','pdf','pdf.svg'],
      ['Gambar','image','img.svg'],['Video','video','vid.svg'],['Audio','audio','aud.svg'],['Lainnya','other','other.svg']
    ];
    var h='';
    for(var i=0;i<types.length;i++){var t=types[i];h+='<div class="type-item" role="button" tabindex="0" data-kind="'+t[1]+'" onclick="openFileType(&quot;'+t[1]+'&quot;)" onkeydown="if(event.key===&quot;Enter&quot;)openFileType(&quot;'+t[1]+'&quot;)"><div class="ti-icon"><img class="cs-icon" src="/icons/file-types/'+t[2]+'" alt=""></div><span class="ti-name">'+t[0]+'</span><span class="ti-size">'+humanSize(z[t[1]]||0)+'</span></div>';};
    document.getElementById('typeList').innerHTML=h;
  });
}

function recentIcon(f,mini){
  var cls=mini?'cs-file-icon':'cs-file-icon';
  if(f.is_pdf)return '<img class="'+cls+'" src="/icons/file-types/pdf.svg" alt="">';
  if(f.is_image)return '<img class="cs-icon md" src="/icons/ui/photo.svg" alt="">';
  if(f.is_video)return '<img class="'+cls+'" src="/icons/file-types/mp4.svg" alt="">';
  if(f.is_audio)return '<img class="'+cls+'" src="/icons/file-types/mp3.svg" alt="">';
  var n=(f.name||'').toLowerCase(),x='other.svg';
  if(n.endsWith('.doc')||n.endsWith('.docx'))x='docx.svg';else if(n.endsWith('.xls')||n.endsWith('.xlsx'))x='xlsx.svg';else if(n.endsWith('.ppt')||n.endsWith('.pptx'))x='pptx.svg';else if(n.endsWith('.zip'))x='zip.svg';else if(n.endsWith('.rar'))x='rar.svg';else if(n.endsWith('.txt'))x='txt.svg';
  return '<img class="'+cls+'" src="/icons/file-types/'+x+'" alt="">';
}

function renderFileRows(files,total){
  var tbody=document.getElementById('filesBody'),empty=document.getElementById('filesEmpty'),table=document.getElementById('filesTable');
  if(!files||!files.length){tbody.innerHTML='';empty.style.display='block';table.style.display='none';return;}
  empty.style.display='none';table.style.display='table';document.getElementById('fileTitle').textContent='File Terbaru';
  var h='';
  for(var i=0;i<files.length;i++){var f=files[i],thumb=f.is_image?'<img src="/api/thumb/'+f.id+'" width="28" height="28" loading="lazy">':'<div class="ficon">'+recentIcon(f,false)+'</div>';
    h+='<tr onclick="openRecentFile('+f.id+','+f.folder_id+')" style="cursor:pointer"><td><div class="fname-cell">'+thumb+'<span>'+escHtml(f.name)+'</span></div></td><td><span class="owner-badge">Saya</span></td><td>'+f.size_human+'</td><td>'+f.uploaded+'</td><td><button class="act-btn fav-btn '+(f.is_favorite?'active':'')+'" data-action="favorite" data-id="'+f.id+'" title="'+(f.is_favorite?'Hapus dari Favorites':'Tambahkan ke Favorites')+'">'+(f.is_favorite?'&#9733;':'&#9734;')+'</button><a class="act-btn" href="/api/download/'+f.id+'" onclick="event.stopPropagation()" title="Download"><img class="cs-icon sm" src="/icons/ui/download.svg" alt="Download"></a><button class="del-btn" data-action="delFile" data-id="'+f.id+'" data-name="'+escHtml(f.name)+'" title="Pindah ke Trash">&#10005;</button></td></tr>';}
  tbody.innerHTML=h;
}

function loadRecentDashboard(){
  currentView='recent';
  currentFolder=-1;
  currentPage=1;
  loadNavPage();
}

var allFoldersVisible=false;
function toggleAllFolders(){
  var el=document.getElementById('foldersGrid'),btn=document.getElementById('folderToggle');
  allFoldersVisible=!allFoldersVisible;
  el.classList.toggle('show-all',allFoldersVisible);
  btn.textContent=allFoldersVisible?'Tutup':'Lihat Semua';
  if(!allFoldersVisible)el.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function loadFolders(){
  api('/api/folders?parent_id='+currentFolder).then(function(d){
    var el=document.getElementById('foldersGrid'),btn=document.getElementById('folderToggle');
    allFoldersVisible=false;el.classList.remove('show-all');btn.textContent='Lihat Semua';
    if(!d||!d.folders||!d.folders.length){el.innerHTML='';btn.style.display='none';document.getElementById('folderTitle').textContent='Folder Saya';return;}
    document.getElementById('folderTitle').textContent='Folder Saya ('+d.folders.length+')';
    btn.style.display=d.folders.length>4?'block':'none';
    var html='';
    for(var i=0;i<d.folders.length;i++){
      var f=d.folders[i];
      html+='<div class="folder-card" onclick="goFolder('+f.id+')">'+
        '<span class="fc-icon"><img class="cs-icon lg" src="/icons/ui/folder.svg" alt=""></span>'+
        '<div class="fc-info"><div class="fc-name" title="'+escHtml(f.name)+'">'+escHtml(f.name)+'</div><div class="fc-count">'+(f.item_count||0)+' item</div></div>'+
        '<span class="fc-menu"><img class="cs-icon sm" src="/icons/ui/more.svg" alt=""></span>'+
        '<button class="del-btn" data-action="delFolder" data-id="'+f.id+'" data-name="'+escHtml(f.name)+'">&#10005;</button></div>';
    }
    el.innerHTML=html;
  });
}

function loadBreadcrumb(){
  if(currentFolder===0){document.getElementById('breadcrumb').innerHTML='<a onclick="goFolder(0)">Home</a>';return;}
  api('/api/folders/breadcrumb/'+currentFolder).then(function(d){
    if(!d||!d.breadcrumb)return;
    var h='<a onclick="goFolder(0)">Home</a>';
    for(var i=0;i<d.breadcrumb.length;i++){
      h+='<span>/</span><a onclick="goFolder('+d.breadcrumb[i].id+')">'+escHtml(d.breadcrumb[i].name)+'</a>';
    }
    document.getElementById('breadcrumb').innerHTML=h;
  });
}

function markActiveView(view){
  document.querySelectorAll('.stat').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-view')===view);});
  document.querySelectorAll('.type-item').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-kind')===view);});
  document.querySelectorAll('.view-btn').forEach(function(x){x.classList.remove('active');});
}
function setViewMode(mode,btn){
  document.querySelectorAll('.view-btn').forEach(function(x){x.classList.remove('active');});
  if(btn)btn.classList.add('active');
}
function loadCurrentView(){
  if(currentView==='type')return loadFileTypePage();
  if(['recent','favorites','shared','trash'].indexOf(currentView)>=0)return loadNavPage();
  loadFiles();
}
function loadFiles(){
  currentView='folder';
  var q=document.getElementById('searchInput').value.trim();
  var url='/api/files?page='+currentPage+'&per_page='+perPage+'&folder_id='+currentFolder;
  if(q)url+='&q='+encodeURIComponent(q);
  api(url).then(function(d){
    var tbody=document.getElementById('filesBody');
    var empty=document.getElementById('filesEmpty');
    var table=document.getElementById('filesTable');
    if(!d||!d.files||!d.files.length){tbody.innerHTML='';empty.style.display='block';table.style.display='none';document.getElementById('pagination').innerHTML='';document.getElementById('fileTitle').textContent='File Terbaru';return;}
    empty.style.display='none';table.style.display='table';
    document.getElementById('fileTitle').textContent='File Terbaru ('+d.total+')';
    var html='';
    for(var i=0;i<d.files.length;i++){
      var f=d.files[i];
      var thumb='';
      if(f.is_image){thumb='<img src="/api/thumb/'+f.id+'" width="36" height="36" loading="lazy">';}
      else if(f.is_video){thumb='<div class="ficon"><img class="cs-file-icon" src="/icons/file-types/mp4.svg" alt=""></div>';}
      else if(f.is_audio){thumb='<div class="ficon"><img class="cs-file-icon" src="/icons/file-types/mp3.svg" alt=""></div>';}
      else if(f.is_pdf){thumb='<div class="ficon"><img class="cs-file-icon" src="/icons/file-types/pdf.svg" alt=""></div>';}
      else{thumb='<div class="ficon"><img class="cs-file-icon" src="/icons/file-types/other.svg" alt=""></div>';}
      html+='<tr onclick="openFile('+f.id+')" style="cursor:pointer"><td><div class="fname-cell">'+thumb+'<span>'+escHtml(f.name)+'</span></div></td>'+
        '<td><span class="owner-badge">Saya</span></td>'+
        '<td>'+f.size_human+'</td><td>'+f.uploaded+'</td>'+
        '<td><a class="act-btn" href="/api/download/'+f.id+'" onclick="event.stopPropagation()" title="Download"><img class="cs-icon sm" src="/icons/ui/download.svg" alt="Download"></a>'+
        '<button class="del-btn" data-action="delFile" data-id="'+f.id+'" data-name="'+escHtml(f.name)+'" title="Hapus">&#128465;</button></td></tr>';
    }
    tbody.innerHTML=html;
    renderPagination(d.page,d.pages);
    // Recent files in right sidebar
    var rhtml='';
    for(var j=0;j<Math.min(3,d.files.length);j++){
      var rf=d.files[j];
      var ri='';
      if(rf.is_pdf)ri='<div class="rm-icon"><img class="cs-file-icon" src="/icons/file-types/pdf.svg" alt=""></div>';
      else if(rf.is_image)ri='<div class="rm-icon"><img class="cs-icon md" src="/icons/ui/photo.svg" alt=""></div>';
      else ri='<div class="rm-icon"><img class="cs-icon md" src="/icons/ui/file.svg" alt=""></div>';
      rhtml+='<div class="rm">'+ri+'<div class="rm-info"><div class="rm-name" title="'+escHtml(rf.name)+'">'+escHtml(rf.name)+'</div><div class="rm-date">'+rf.uploaded+'</div></div></div>';
    }
    document.getElementById('recentMini').innerHTML=rhtml;
  });
}

function loadFileTypePage(){
  var q=document.getElementById('searchInput').value.trim();
  var url='/api/files/type/'+encodeURIComponent(currentType)+'?page='+currentPage+'&per_page='+perPage;
  if(q)url+='&q='+encodeURIComponent(q);
  api(url).then(function(d){
    if(!d||!d.files)return;
    renderFileRows(d.files,d.total);
    document.getElementById('fileTitle').textContent=(currentType||'File')+' ('+d.total+')';
    renderPagination(d.page,d.pages);
  });
}
function renderPagination(page,pages){
  var el=document.getElementById('pagination');
  if(pages<=1){el.innerHTML='';return;}
  var h='';
  if(page>1)h+='<button class="btn" onclick="goPage('+(page-1)+')">&#8249;</button>';
  var s=Math.max(1,page-2),e=Math.min(pages,page+2);
  for(var i=s;i<=e;i++)h+='<button class="btn'+(i===page?' active':'')+'" onclick="goPage('+i+')">'+i+'</button>';
  if(page<pages)h+='<button class="btn" onclick="goPage('+(page+1)+')">&#8250;</button>';
  el.innerHTML=h;
}

function goFolder(id){currentFolder=id;currentPage=1;loadFolders();if(id===0)loadRecentDashboard();else loadFiles();loadBreadcrumb();}
function openAllFiles(){
  var item=document.querySelector('.nav-item[data-nav="recent"]');
  markActiveView('files');
  openNav('recent',item);
}
function openStorageFiles(){
  var item=document.querySelector('.nav-item[data-nav="recent"]');
  markActiveView('size');
  openNav('recent',item);
}
function openFolderOverview(){
  var item=document.querySelector('.nav-item[data-nav="dashboard"]');
  markActiveView('folders');
  openNav('dashboard',item);
  document.getElementById('foldersGrid').scrollIntoView({behavior:'smooth',block:'center'});
}
function openFileType(kind){
  var labels={document:'Dokumen',spreadsheet:'Spreadsheet',presentation:'Presentasi',pdf:'PDF',image:'Gambar',video:'Video',audio:'Audio',other:'Lainnya'};
  currentView='type';currentType=kind;currentPage=1;currentFolder=-1;markActiveView(kind);
  document.getElementById('folderTitle').textContent='Tipe File: '+(labels[kind]||kind);
  document.getElementById('foldersGrid').innerHTML='';
  document.getElementById('folderToggle').style.display='none';
  document.getElementById('breadcrumb').innerHTML='<a>Tipe File</a><span>/</span><a>'+escHtml(labels[kind]||kind)+'</a>';
  loadFileTypePage();
}
function showAllFileTypes(e){
  e.preventDefault();
  document.querySelector('.right-sidebar').scrollIntoView({behavior:'smooth',block:'start'});
  document.getElementById('typeList').focus();
}
function showAllRecent(e){
  e.preventDefault();
  var item=document.querySelector('.nav-item[data-nav="recent"]');
  openNav('recent',item);
}
function loadNavPage(){
  var q=document.getElementById('searchInput').value.trim();
  var url='/api/navigation/'+currentView+'?page='+currentPage+'&per_page='+perPage;
  if(q)url+='&q='+encodeURIComponent(q);
  api(url).then(function(d){
    if(!d||d.error)return;
    renderFileRows(d.files,d.total);
    var labels={recent:'File Terbaru',favorites:'Favorites',shared:'Shared with me',trash:'Trash'};
    document.getElementById('fileTitle').textContent=(labels[currentView]||'File')+' ('+d.total+')';
    renderPagination(d.page,d.pages);
  });
}
function openNav(section,el){
  document.querySelectorAll('.nav-item').forEach(function(x){x.classList.remove('active');});
  if(el)el.classList.add('active');
  if(section==='dashboard'){currentView='dashboard';currentFolder=0;currentPage=1;markActiveView('dashboard');loadAll();loadBreadcrumb();return;}
  currentView=section;currentPage=1;currentFolder=-1;
  document.getElementById('folderTitle').textContent=section==='recent'?'Recent':section==='favorites'?'Favorites':section==='shared'?'Shared with me':'Trash';
  document.getElementById('foldersGrid').innerHTML='';
  document.getElementById('folderToggle').style.display='none';
  document.getElementById('breadcrumb').innerHTML='<a>'+document.getElementById('folderTitle').textContent+'</a>';
  loadNavPage();
}
function goPage(p){currentPage=p;loadCurrentView();window.scrollTo({top:0,behavior:'smooth'});}
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function changePageSize(){
  var el=document.getElementById('pageSize');
  perPage=parseInt(el.value,10)||25;
  el.classList.add('active');
  currentPage=1;
  loadCurrentView();
}
function debounceSearch(){clearTimeout(searchTimer);searchTimer=setTimeout(function(){currentPage=1;loadCurrentView();},300);}
function humanSize(b){if(b===0)return'0 B';var u=['B','KB','MB','GB','TB'];var i=Math.floor(Math.log(b)/Math.log(1024));return(b/Math.pow(1024,i)).toFixed(1)+' '+u[i];}

// Event delegation for delete
document.addEventListener('click',function(e){
  var btn=e.target.closest('[data-action]');
  if(!btn)return;
  e.stopPropagation();
  var act=btn.getAttribute('data-action'),id=btn.getAttribute('data-id'),name=btn.getAttribute('data-name');
  if(act==='delFolder'){
    if(confirm('Hapus folder "'+name+'" dan semua isinya?'))api('/api/folders/'+id,{method:'DELETE'}).then(function(){loadAll();});
  }else if(act==='favorite'){
    toggleFavorite(id);
  }else if(act==='delFile'){
    if(confirm('Pindahkan "'+name+'" ke Trash? File di Telegram tetap aman dan bisa dipulihkan.'))api('/api/delete/'+id,{method:'DELETE'}).then(function(){loadCurrentView();loadStats();});
  }
});
function toggleFavorite(id){
  api('/api/files/'+id+'/favorite',{method:'POST'}).then(function(d){
    if(d&&d.ok){loadCurrentView();}
  });
}

// Upload
function triggerUpload(){document.getElementById('fileInput').click();}
function setupDragDrop(){
  var dz=document.getElementById('dropZone'),fi=document.getElementById('fileInput');
  dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('dragover');});
  dz.addEventListener('dragleave',function(){dz.classList.remove('dragover');});
  dz.addEventListener('drop',function(e){e.preventDefault();dz.classList.remove('dragover');uploadFiles(e.dataTransfer.files);});
  fi.addEventListener('change',function(){if(fi.files.length)uploadFiles(fi.files);fi.value='';});
}
function uploadFiles(fileList){
  var prog=document.getElementById('uploadProgress'),status=document.getElementById('uploadStatus'),fill=document.getElementById('progressFill');
  prog.style.display='block';
  var fd=new FormData(),count=0;
  for(var i=0;i<fileList.length;i++){fd.append('files',fileList[i]);count++;}
  fd.append('folder_id',currentFolder);
  status.textContent='Mengupload '+count+' file...';fill.style.width='0%';
  var xhr=new XMLHttpRequest();
  xhr.upload.addEventListener('progress',function(e){if(e.lengthComputable)fill.style.width=Math.round(e.loaded/e.total*100)+'%';});
  xhr.onload=function(){status.textContent='Upload selesai!';fill.style.width='100%';setTimeout(function(){prog.style.display='none';},2000);loadAll();};
  xhr.onerror=function(){status.textContent='Upload gagal!';};
  xhr.open('POST','/api/upload');xhr.send(fd);
}

// Modals
function showNewFolderModal(){document.getElementById('newFolderModal').classList.add('show');document.getElementById('newFolderName').value='';document.getElementById('newFolderName').focus();}
function createFolder(){var n=document.getElementById('newFolderName').value.trim();if(!n)return;api('/api/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,parent_id:currentFolder})}).then(function(d){if(d.ok){closeModal('newFolderModal');loadFolders();loadStats();}else if(d.error)alert(d.error);});}
function closeModal(id){document.getElementById(id).classList.remove('show');}

// Select
function toggleSelectMode(){selectMode=!selectMode;selectedIds.clear();document.getElementById('selectedBar').classList.toggle('show',selectMode);document.getElementById('selectBtn').textContent=selectMode?'Cancel':'\u2611 Select';updateSelectedCount();}
function updateSelectedCount(){document.getElementById('selectedCount').textContent=selectedIds.size+' dipilih';}
function deleteSelected(){if(!selectedIds.size)return;if(!confirm('Hapus '+selectedIds.size+' file?'))return;var p=[];selectedIds.forEach(function(id){p.push(api('/api/delete/'+id,{method:'DELETE'}));});Promise.all(p).then(function(){selectedIds.clear();loadAll();updateSelectedCount();});}

// Move
function moveSelected(){
  if(!selectedIds.size)return;
  api('/api/folders?parent_id='+currentFolder).then(function(d){
    var h='<div class="folder-card" onclick="doMove(0)" style="margin-bottom:8px;cursor:pointer"><span class="fc-icon">&#127968;</span><div class="fc-info"><div class="fc-name">Root (Home)</div></div></div>';
    if(d&&d.folders)for(var i=0;i<d.folders.length;i++){var f=d.folders[i];h+='<div class="folder-card" onclick="doMove('+f.id+')" style="margin-bottom:8px;cursor:pointer"><span class="fc-icon">&#128193;</span><div class="fc-info"><div class="fc-name">'+escHtml(f.name)+'</div></div></div>';}
    document.getElementById('moveFolderList').innerHTML=h;
    document.getElementById('moveModal').classList.add('show');
  });
}
function doMove(tf){var ids=[];selectedIds.forEach(function(id){ids.push(parseInt(id));});api('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids:ids,folder_id:tf})}).then(function(){closeModal('moveModal');selectedIds.clear();loadAll();updateSelectedCount();toggleSelectMode();});}

// Preview
function openRecentFile(id,folderId){currentFolder=folderId;openFile(id);}
function openFile(id){
  api('/api/files?page=1&folder_id='+currentFolder).then(function(d){
    if(!d||!d.files)return;var file=d.files.find(function(f){return f.id===id;});if(!file)return;
    var el=document.getElementById('previewContent');
    if(file.is_image){el.innerHTML='<img src="/api/preview/'+id+'" style="max-width:90vw;max-height:80vh">';}
    else if(file.is_video){el.innerHTML='<video src="/api/preview/'+id+'" controls style="max-width:90vw;max-height:80vh"></video>';}
    else if(file.is_audio){el.innerHTML='<audio src="/api/preview/'+id+'" controls></audio>';}
    else if(file.is_pdf){el.innerHTML='<iframe src="/api/preview/'+id+'" style="width:90vw;height:85vh;border:none;border-radius:8px"></iframe>';}
    else{location.href='/api/download/'+id;return;}
    document.getElementById('filePreview').classList.add('show');
  });
}

init();
</script>
</body>
</html>'''

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("[*] Initializing DB...")
    init_db()
    print("[*] Initializing Telethon...")
    loop = get_loop()
    future = asyncio.run_coroutine_threadsafe(_init_telethon(), loop)
    future.result(timeout=30)
    print("[*] Starting web server on port 8050...")
    register_drive_features(app)
app.run(host="0.0.0.0", port=8050, debug=False)
