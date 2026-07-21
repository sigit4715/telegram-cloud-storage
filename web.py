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

from flask import Flask, request, jsonify, send_file, redirect, url_for, session
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
    # Migrate: add folder_id if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    if "folder_id" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER DEFAULT 0")
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
        is_allowed = (uid and uid in ALLOWED) or (g_email and g_email in GOOGLE_ALLOWED_EMAILS)
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
    rows = db_query("SELECT id, name, created_at FROM folders WHERE parent_id=? ORDER BY name", (parent,))
    return jsonify({"folders": [{"id": r["id"], "name": r["name"], "created": r["created_at"]} for r in rows]})

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
@app.route("/api/files")
@login_required
def api_files():
    q = request.args.get("q", "").strip()
    folder_id = int(request.args.get("folder_id", 0))
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    offset = (page - 1) * per_page

    params = []
    where = "WHERE folder_id=?"
    params.append(folder_id)

    if q:
        where += " AND file_name LIKE ?"
        params.append(f"%{q}%")

    total = db_scalar(f"SELECT COUNT(*) FROM files {where}", params)
    rows = db_query(
        f"SELECT id, file_name, size, mime, uploaded_at, folder_id FROM files {where} ORDER BY id DESC LIMIT ? OFFSET ?",
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
    row = db_query("SELECT msg_id FROM files WHERE id=?", (fid,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        run_async(telethon_client.delete_messages(CHANNEL, [row[0]["msg_id"]]))
    except:
        pass
    db_exec("DELETE FROM files WHERE id=?", (fid,))
    return jsonify({"ok": True})

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
    return jsonify({
        "total_files": total_files,
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "images": images,
        "videos": videos,
        "audios": audios,
        "others": others,
        "folders": folders,
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
:root{--bg:#0d0221;--card:#1a0a3e;--border:#6c3baa;--accent:#a78bfa;--accent2:#8b5cf6;--text:#e0e0e0;--muted:#6b7280;--danger:#f87171;--green:#34d399;--yellow:#fbbf24}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* Topbar */
.topbar{background:#1a0a3e;border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent)}
.topbar .right{display:flex;align-items:center;gap:12px}
.topbar .right span{font-size:13px;color:var(--muted)}
.topbar .right button{padding:6px 14px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}

/* Stats */
.stats{display:flex;gap:12px;padding:16px 24px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 18px;min-width:120px}
.stat .num{font-size:24px;font-weight:700;color:var(--accent)}
.stat .lbl{font-size:11px;color:var(--muted);margin-top:2px}

/* Toolbar */
.toolbar{padding:0 24px 12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.search{flex:1;min-width:200px;padding:10px 16px;background:var(--card);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none}
.search:focus{border-color:var(--accent)}
.btn-new{padding:10px 18px;background:linear-gradient(135deg,#6c3baa,#a78bfa);color:#fff;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}

/* Breadcrumb */
.breadcrumb{padding:0 24px 12px;display:flex;gap:4px;flex-wrap:wrap;align-items:center;font-size:14px}
.breadcrumb a{color:var(--accent);text-decoration:none;cursor:pointer}
.breadcrumb a:hover{text-decoration:underline}
.breadcrumb .sep{color:var(--muted);margin:0 2px}
.breadcrumb .current{color:var(--text);font-weight:600}

/* Upload zone */
.upload-zone{border:2px dashed var(--border);border-radius:12px;padding:24px;text-align:center;margin:0 24px 16px;cursor:pointer;transition:.2s}
.upload-zone:hover,.upload-zone.drag{border-color:var(--accent);background:rgba(167,139,250,.05)}
.upload-zone svg{width:36px;height:36px;stroke:var(--accent);margin-bottom:6px}
.upload-zone p{color:var(--muted);font-size:13px}

/* Upload progress panel */
.upload-panel{display:none;margin:0 24px 16px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;max-height:300px;overflow-y:auto}
.upload-panel.show{display:block}
.upload-panel h3{font-size:14px;color:var(--accent);margin-bottom:12px}
.upload-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(108,59,170,.2)}
.upload-item:last-child{border-bottom:none}
.upload-item .icon{font-size:16px;flex-shrink:0}
.upload-item .info{flex:1;min-width:0}
.upload-item .name{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.upload-item .progress-bar{height:4px;background:#2d1b69;border-radius:2px;margin-top:4px;overflow:hidden}
.upload-item .progress-bar .fill{height:100%;border-radius:2px;transition:width .3s}
.upload-item .progress-bar .fill.uploading{background:var(--accent);animation:pulse 1.5s infinite}
.upload-item .progress-bar .fill.done{background:var(--green)}
.upload-item .progress-bar .fill.fail{background:var(--danger)}
.upload-item .status{font-size:11px;flex-shrink:0;white-space:nowrap}
.upload-item .status.uploading{color:var(--accent)}
.upload-item .status.done{color:var(--green)}
.upload-item .status.fail{color:var(--danger)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* Folders grid */
.folders{padding:0 24px;display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px}
.folder{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;cursor:pointer;transition:.2s;display:flex;align-items:center;gap:8px;min-width:140px}
.folder:hover{border-color:var(--accent);transform:translateY(-1px)}
.folder .icon{font-size:20px}
.folder .name{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.folder .del-btn{opacity:0;background:none;border:none;color:var(--danger);cursor:pointer;font-size:14px;transition:.2s;padding:2px 6px;border-radius:4px}
.folder:hover .del-btn{opacity:1}
.folder .del-btn:hover{background:rgba(248,113,113,.1)}

/* Files grid */
.files{padding:0 24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.file{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;cursor:pointer;transition:.2s;position:relative}
.file:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 16px rgba(108,59,170,.3)}
.file .thumb{width:100%;height:140px;background:#0d0221;display:flex;align-items:center;justify-content:center;overflow:hidden}
.file .thumb img{width:100%;height:100%;object-fit:cover}
.file .thumb svg{width:44px;height:44px;stroke:var(--muted)}
.file .info{padding:10px}
.file .info .name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file .info .meta{font-size:10px;color:var(--muted);margin-top:3px;display:flex;justify-content:space-between}
.file .actions{position:absolute;top:6px;right:6px;display:flex;gap:3px;opacity:0;transition:.2s}
.file:hover .actions{opacity:1}
.file .actions button{width:26px;height:26px;border-radius:6px;border:none;background:rgba(0,0,0,.6);color:#fff;cursor:pointer;font-size:11px;display:flex;align-items:center;justify-content:center}

/* Select mode */
.select-bar{display:none;position:sticky;top:52px;z-index:99;background:var(--card);border-bottom:1px solid var(--border);padding:10px 24px;align-items:center;gap:12px}
.select-bar.show{display:flex}
.select-bar .count{color:var(--accent);font-size:14px;font-weight:600}
.select-bar button{padding:6px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--accent);cursor:pointer;font-size:13px}
.select-bar button.danger{border-color:var(--danger);color:var(--danger)}
.select-bar button.primary{background:var(--accent);color:#fff;border:none}
.file .checkbox{position:absolute;top:6px;left:6px;width:22px;height:22px;border-radius:6px;border:2px solid var(--border);background:rgba(0,0,0,.4);cursor:pointer;display:none;align-items:center;justify-content:center;font-size:12px;color:#fff;z-index:5}
.select-mode .file .checkbox{display:flex}
.file .checkbox.checked{background:var(--accent);border-color:var(--accent)}
.folder.drop-hover{border-color:var(--green)!important;background:rgba(52,211,153,.1)!important}

/* Folder Picker Modal */
.picker-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:200}
.picker-overlay.show{display:flex}
.picker-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;width:90%;max-width:440px;max-height:70vh;overflow-y:auto}
.picker-box h3{color:var(--accent);margin-bottom:16px;font-size:18px}
.picker-item{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:10px;cursor:pointer;transition:.15s;border:1px solid transparent}
.picker-item:hover{background:var(--bg);border-color:var(--border)}
.picker-item .icon{font-size:24px}
.picker-item .name{flex:1;color:var(--text);font-size:14px}
.picker-item .arrow{color:var(--muted);font-size:12px}
.picker-home{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:10px;cursor:pointer;border:1px solid var(--accent);margin-bottom:12px;color:var(--accent);font-weight:600}
.picker-home:hover{background:var(--accent);color:#fff}
.picker-search{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;margin-bottom:12px;outline:none}
.picker-search:focus{border-color:var(--accent)}
/* Empty */
.empty{text-align:center;padding:40px 20px;color:var(--muted)}
.empty svg{width:56px;height:56px;stroke:var(--border);margin-bottom:8px}

/* Pagination */
.pagination{display:flex;justify-content:center;gap:6px;padding:20px}
.pagination button{padding:6px 14px;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--accent);cursor:pointer;font-size:13px}
.pagination button.active{background:var(--accent);color:#fff}
.pagination button:disabled{opacity:.3;cursor:not-allowed}

/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:200;align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-content{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:90vw;max-height:95vh;position:relative;min-width:300px;transition:max-width .2s}
.modal-content img,.modal-content video{max-width:100%;max-height:70vh;border-radius:8px}
.modal-close{position:absolute;top:8px;right:12px;background:none;border:none;color:#fff;font-size:24px;cursor:pointer}
.modal-actions{display:flex;gap:8px;margin-top:16px;justify-content:center}
.modal-actions button{padding:8px 20px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--accent);cursor:pointer;font-size:13px}
.modal-actions button.danger{border-color:var(--danger);color:var(--danger)}

/* New folder modal */
.new-folder-input{padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;width:100%;margin:12px 0;outline:none}
.new-folder-input:focus{border-color:var(--accent)}
.modal-buttons{display:flex;gap:8px;justify-content:flex-end}
.modal-buttons button{padding:8px 18px;border-radius:8px;border:none;cursor:pointer;font-size:13px}
.modal-buttons .primary{background:var(--accent);color:#fff}
.modal-buttons .secondary{background:transparent;border:1px solid var(--border);color:var(--accent)}
</style>
</head>
<body>

<div class="topbar">
  <h1>☁️ Cloud Storage</h1>
  <div class="right">
    <span id="userInfo"></span>
    <a href="/profile" style="color:#a78bfa;text-decoration:none;font-size:13px;padding:6px 12px;border:1px solid #a78bfa;border-radius:8px">👤 Profile</a>
    <a href="/gdrive" style="text-decoration:none;font-size:13px;padding:6px 12px;border:1px solid #34a853;color:#34a853;border-radius:8px">🔌 Google Drive</a>
    <a href="/photos" style="text-decoration:none;font-size:13px;padding:6px 12px;border:1px solid #e879a0;color:#e879a0;border-radius:8px">📷 Google Photos</a>
    <button onclick="doLogout()">Logout</button>
  </div>
</div>

<div class="stats" id="stats"></div>

<div class="breadcrumb" id="breadcrumb"></div>

<div class="toolbar">
  <input class="search" id="searchInput" placeholder="🔍 Cari file..." oninput="debounceSearch()">
  <button class="btn-new" onclick="showNewFolder()">📁 New Folder</button>
  <button class="btn-new" onclick="toggleSelectMode()">☑ Select</button>
  <button class="btn-new" onclick="document.getElementById('fileInput').click()">⬆ Upload</button>
  <input type="file" id="fileInput" multiple hidden>
</div>

<div class="upload-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
  <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 20h16"/></svg>
  <p>Seret file ke sini atau klik untuk upload</p>
</div>

<div class="upload-panel" id="uploadPanel">
  <h3>📤 Upload Progress</h3>
  <div id="uploadList"></div>
</div>

<div class="select-bar" id="selectBar">
  <span class="count" id="selectCount">0 dipilih</span>
  <button onclick="moveSelected()">📁 Pindah ke Folder</button>
  <button class="danger" onclick="deleteSelected()">🗑 Hapus</button>
  <button onclick="toggleSelectMode()">✕ Batal</button>
</div>

<div class="picker-overlay" id="pickerOverlay" onclick="if(event.target===this)closePicker()">
  <div class="picker-box">
    <h3>Pindah ke Folder</h3>
    <input class="picker-search" id="pickerSearch" placeholder="Cari folder..." oninput="filterPicker()">
    <div class="picker-home" onclick="pickerMoveTo(0)">Home (Root)</div>
    <div id="pickerFolders"></div>
  </div>
</div>
<div class="folders" id="foldersGrid"></div>
<div class="files" id="filesGrid"></div>
<div class="pagination" id="pagination"></div>

<!-- Preview Modal -->
<div class="modal" id="modal">
  <div class="modal-content" id="modalContent">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div id="previewArea"></div>
    <div class="modal-actions">
      <button onclick="downloadCurrent()">⬇ Download</button>
      <button class="danger" onclick="deleteCurrent()">🗑 Hapus</button>
    </div>
  </div>
</div>

<!-- New Folder Modal -->
<div class="modal" id="folderModal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeFolderModal()">✕</button>
    <h3 style="color:var(--accent);margin-bottom:4px">📁 New Folder</h3>
    <input class="new-folder-input" id="folderNameInput" placeholder="Nama folder..." maxlength="100">
    <div class="modal-buttons">
      <button class="secondary" onclick="closeFolderModal()">Batal</button>
      <button class="primary" onclick="createFolder()">Buat</button>
    </div>
  </div>
</div>

<script>
let currentPage=1, currentQ='', currentFolder=0, currentFile=null, searchTimer=null;
let breadcrumbPath=[];

const api=(url,opts)=>fetch(url,{credentials:'same-origin',...opts}).then(r=>r.json());

async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  document.getElementById('userInfo').textContent='ID: '+me.user_id;
  loadAll();
}

async function loadAll(){
  loadStats();
  loadFolders();
  loadFiles();
  loadBreadcrumb();
}

// === STATS ===
async function loadStats(){
  const s=await api('/api/stats');
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="num">${s.total_files}</div><div class="lbl">📁 File</div></div>
    <div class="stat"><div class="num">${s.total_size_human}</div><div class="lbl">💾 Ukuran</div></div>
    <div class="stat"><div class="num">${s.folders}</div><div class="lbl">📂 Folder</div></div>
    <div class="stat"><div class="num">${s.images}</div><div class="lbl">🖼️ Foto</div></div>
    <div class="stat"><div class="num">${s.videos}</div><div class="lbl">🎬 Video</div></div>
    <div class="stat"><div class="num">${s.audios}</div><div class="lbl">🎵 Audio</div></div>`;
}

// === BREADCRUMB ===
async function loadBreadcrumb(){
  const el=document.getElementById('breadcrumb');
  let html='';
  if(currentFolder>0){
    const d=await api('/api/folders/breadcrumb/'+currentFolder);
    let parentId=0;
    if(d.breadcrumb.length>1) parentId=d.breadcrumb[d.breadcrumb.length-2].id;
    html+='<button onclick="goFolder('+parentId+')" style="background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--accent);padding:6px 14px;cursor:pointer;font-size:13px;margin-right:8px">◀ Back</button>';
  }
  html+='<a onclick="goFolder(0)" style="cursor:pointer">🏠 Home</a>';
  if(currentFolder>0){
    const d=await api('/api/folders/breadcrumb/'+currentFolder);
    for(const b of d.breadcrumb){
      html+='<span class="sep">/</span><a onclick="goFolder('+b.id+')" style="cursor:pointer">'+b.name+'</a>';
    }
  }
  el.innerHTML=html;
}

// === FOLDERS ===
async function loadFolders(){
  const d=await api('/api/folders?parent_id='+currentFolder);
  const el=document.getElementById('foldersGrid');
  if(!d.folders.length){el.innerHTML='';return;}
  el.innerHTML=d.folders.map(f=>`
    <div class="folder" ondblclick="goFolder(${f.id})" ondragover="onFolderDragOver(event)" ondragleave="onFolderDragLeave(event)" ondrop="onFolderDrop(event,${f.id})">
      <span class="icon">📁</span>
      <span class="name">${f.name}</span>
      <button class="del-btn" onclick="event.stopPropagation();deleteFolder(${f.id},'${f.name.replace(/'/g,"\\\\'")}')" title="Hapus folder">✕</button>
    </div>`).join('');
}

function goFolder(id){currentFolder=id;currentPage=1;currentQ='';document.getElementById('searchInput').value='';loadAll();}

async function showNewFolder(){document.getElementById('folderModal').classList.add('show');document.getElementById('folderNameInput').value='';document.getElementById('folderNameInput').focus();}
function closeFolderModal(){document.getElementById('folderModal').classList.remove('show');}

async function createFolder(){
  const name=document.getElementById('folderNameInput').value.trim();
  if(!name)return;
  await api('/api/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,parent_id:currentFolder})});
  closeFolderModal();
  loadAll();
}

async function deleteFolder(id,name){
  if(!confirm('Hapus folder "'+name+'" dan semua isinya?'))return;
  await api('/api/folders/'+id,{method:'DELETE'});
  loadAll();
}

// === FILES ===
async function loadFiles(){
  const url='/api/files?page='+currentPage+'&q='+encodeURIComponent(currentQ)+'&folder_id='+currentFolder;
  const d=await api(url);
  const g=document.getElementById('filesGrid');
  if(!d.files.length){
    g.innerHTML=currentFolder>0||currentQ?'<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg><p>Tidak ada file</p></div>':'<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg><p>Belum ada file. Upload file pertama!</p></div>';
  } else {
    g.innerHTML=d.files.map(f=>{
      let thumb='';
      if(f.is_image) thumb='<img src="/api/thumb/'+f.id+'" loading="lazy">';
      else if(f.is_video) thumb='<svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><polygon points="5,3 19,12 5,21"/></svg>';
      else if(f.is_audio) thumb='<svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
      else if(f.is_pdf) thumb='<svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><text x="7" y="17" font-size="6" fill="#f87171" stroke="none" font-weight="bold">PDF</text></svg>';
      return'<div class="file" draggable="true" ondragstart="onFileDragStart(event,'+f.id+')" onclick="openFile('+f.id+',\\''+f.name.replace(/'/g,"\\\\'")+'\\',\\''+f.mime+'\\')">'+
        '<div class="checkbox" onclick="event.stopPropagation();toggleFileSelect('+f.id+',this)"></div>'+
        '<div class="thumb">'+thumb+'</div>'+
        '<div class="info"><div class="name" title="'+f.name+'">'+f.name+'</div>'+
        '<div class="meta"><span>'+f.size_human+'</span><span>'+f.uploaded+'</span></div></div>'+
        '<div class="actions">'+
          '<button onclick="event.stopPropagation();downloadFile('+f.id+')" title="Download">⬇</button>'+
          '<button onclick="event.stopPropagation();deleteFile('+f.id+',\\''+f.name.replace(/'/g,"\\\\'")+'\\')" title="Hapus">🗑</button>'+
        '</div></div>';
    }).join('');
  }
  let pg='';
  if(d.pages>1){
    pg+='<button '+(d.page<=1?'disabled':'')+' onclick="goPage('+(d.page-1)+')">◀</button>';
    for(let i=1;i<=Math.min(d.pages,10);i++) pg+='<button class="'+(i===d.page?'active':'')+'" onclick="goPage('+i+')">'+i+'</button>';
    pg+='<button '+(d.page>=d.pages?'disabled':'')+' onclick="goPage('+(d.page+1)+')">▶</button>';
  }
  document.getElementById('pagination').innerHTML=pg;
}

function goPage(p){currentPage=p;loadFiles();}
function debounceSearch(){clearTimeout(searchTimer);searchTimer=setTimeout(()=>{currentQ=document.getElementById('searchInput').value;currentPage=1;loadFiles();},300);}

function downloadFile(id){window.open('/api/download/'+id);}
function deleteFile(id,name){
  if(!confirm('Hapus "'+name+'"?'))return;
  api('/api/delete/'+id,{method:'DELETE'}).then(()=>loadAll());
}
async function openFile(id,name,mime){
  currentFile={id,name,mime};
  const pa=document.getElementById('previewArea');
  if(mime.startsWith('image/')) pa.innerHTML='<img src="/api/preview/'+id+'" style="max-width:100%;max-height:70vh;border-radius:8px">';
  else if(mime.startsWith('video/')) pa.innerHTML='<video src="/api/preview/'+id+'" controls style="max-width:100%;max-height:70vh;border-radius:8px"></video>';
  else if(mime.startsWith('audio/')) pa.innerHTML='<audio src="/api/preview/'+id+'" controls style="width:100%"></audio>';
  else pa.innerHTML='<div style="padding:40px;text-align:center;color:var(--muted)"><p style="font-size:48px">📄</p><p>'+name+'</p></div>';
  document.getElementById('modal').classList.add('show');
}
function closeModal(){document.getElementById('modal').classList.remove('show');}
function downloadCurrent(){if(currentFile)downloadFile(currentFile.id);}
function deleteCurrent(){if(currentFile){deleteFile(currentFile.id,currentFile.name);closeModal();}}

// === UPLOAD WITH PROGRESS ===
const dz=document.getElementById('dropZone');
const fi=document.getElementById('fileInput');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');uploadFiles(e.dataTransfer.files);});
fi.addEventListener('change',()=>uploadFiles(fi.files));

async function uploadFiles(files){
  if(!files.length)return;
  const panel=document.getElementById('uploadPanel');
  const list=document.getElementById('uploadList');
  panel.classList.add('show');
  list.innerHTML='';

  // Create progress items
  const items=[];
  for(const f of files){
    const id='up_'+Math.random().toString(36).slice(2,8);
    const icon=f.type.startsWith('image/')?'🖼️':f.type.startsWith('video/')?'🎬':f.type.startsWith('audio/')?'🎵':'📄';
    list.innerHTML+='<div class="upload-item" id="'+id+'">'+
      '<span class="icon">'+icon+'</span>'+
      '<div class="info"><div class="name">'+f.name+'</div>'+
      '<div class="progress-bar"><div class="fill uploading" style="width:0%"></div></div></div>'+
      '<span class="status uploading">⏳ Upload...</span></div>';
    items.push({file:f,id});
  }

  // Upload sequentially (Telethon rate limit)
  for(const item of items){
    const el=document.getElementById(item.id);
    if(!el)continue;
    const fill=el.querySelector('.fill');
    const status=el.querySelector('.status');

    try{
      fill.style.width='30%';
      status.textContent='📤 Uploading...';

      const fd=new FormData();
      fd.append('files',item.file);
      fd.append('folder_id',currentFolder);

      fill.style.width='60%';

      const r=await fetch('/api/upload',{method:'POST',body:fd,credentials:'same-origin'});
      const d=await r.json();

      if(d.results && d.results[0] && d.results[0].ok){
        fill.style.width='100%';
        fill.className='fill done';
        status.textContent='✅ '+d.results[0].size_human;
        status.className='status done';
      } else {
        fill.style.width='100%';
        fill.className='fill fail';
        status.textContent='❌ '+(d.results?.[0]?.error||'Gagal');
        status.className='status fail';
      }
    }catch(e){
      fill.style.width='100%';
      fill.className='fill fail';
      status.textContent='❌ Error: '+e.message;
      status.className='status fail';
    }
  }

  // Refresh data
  loadAll();

  // Auto hide panel after 5s
  setTimeout(()=>{panel.classList.remove('show');},5000);
}

async function moveOneFile(){
  moveTarget=[currentFile.id];
  showPicker();
}

async function doLogout(){
  await api('/api/logout',{method:'POST'});
  location.href='/';
}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeFolderModal();}});
document.getElementById('folderNameInput').addEventListener('keydown',e=>{if(e.key==='Enter')createFolder();});
// === SELECT MODE ===
let selectMode=false, selectedIds=new Set();

function toggleSelectMode(){
  selectMode=!selectMode;
  document.body.classList.toggle('select-mode',selectMode);
  document.getElementById('selectBar').classList.toggle('show',selectMode);
  if(!selectMode){selectedIds.clear();updateSelectCount();}
  loadFiles();
}

function toggleFileSelect(id,el){
  if(selectedIds.has(id))selectedIds.delete(id);else selectedIds.add(id);
  el.classList.toggle('checked',selectedIds.has(id));
  updateSelectCount();
}

function updateSelectCount(){
  document.getElementById('selectCount').textContent=selectedIds.size+' dipilih';
}

let moveTarget=[];
let pickerFoldersList=[];

async function moveSelected(){
  if(!selectedIds.size)return;
  moveTarget=[...selectedIds];
  showPicker();
}

async function showPicker(){
  const d=await api('/api/folders');
  pickerFoldersList=d.folders||[];
  renderPickerFolders(pickerFoldersList);
  document.getElementById('pickerOverlay').classList.add('show');
  document.getElementById('pickerSearch').value='';
  document.getElementById('pickerSearch').focus();
}

function renderPickerFolders(folders){
  const el=document.getElementById('pickerFolders');
  el.innerHTML=folders.map(f=>'<div class="picker-item" onclick="pickerMoveTo('+f.id+')"><span class="icon">📁</span><span class="name">'+f.name+'</span><span class="arrow">▸</span></div>').join('');
}

function filterPicker(){
  const q=document.getElementById('pickerSearch').value.toLowerCase();
  const filtered=pickerFoldersList.filter(f=>f.name.toLowerCase().includes(q));
  renderPickerFolders(filtered);
}

function closePicker(){document.getElementById('pickerOverlay').classList.remove('show');}

async function pickerMoveTo(folderId){
  closePicker();
  await api('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids:moveTarget,folder_id:folderId})});
  moveTarget=[];
  selectedIds.clear();updateSelectCount();loadAll();
}

async function deleteSelected(){
  if(!selectedIds.size)return;
  if(!confirm('Hapus '+selectedIds.size+' file?'))return;
  for(const id of selectedIds){await api('/api/delete/'+id,{method:'DELETE'});}
  selectedIds.clear();updateSelectCount();loadAll();
}

// === DRAG & DROP TO FOLDER ===
let dragFileId=null;

function onFileDragStart(e,id){dragFileId=id;e.dataTransfer.effectAllowed='move';}
function onFolderDragOver(e){e.preventDefault();e.currentTarget.classList.add('drop-hover');}
function onFolderDragLeave(e){e.currentTarget.classList.remove('drop-hover');}
async function onFolderDrop(e,folderId){
  e.preventDefault();
  e.currentTarget.classList.remove('drop-hover');
  if(dragFileId===null)return;
  await api('/api/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids:[dragFileId],folder_id:folderId})});
  dragFileId=null;loadAll();
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
