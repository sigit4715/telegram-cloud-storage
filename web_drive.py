#!/usr/bin/env python3
"""
web_drive.py — Profile Page + Google Drive Integration
=======================================================

Drop-in extension module for the Telegram Cloud Storage Flask app (web.py).

It registers a Flask Blueprint `drive_ext` onto the main `app` and adds:
  1. Profile page (/profile) + API (GET/POST /api/profile, POST /api/profile/photo)
  2. Google Drive integration (/gdrive) + OAuth2 flow + file browser + copy-to-Telegram

USAGE (in web.py), placed near the bottom before app.run():
    from web_drive import register_drive_features
    register_drive_features(app)

The module reuses the existing helpers from web.py (db_query, db_exec, db_scalar,
run_async, telethon_client, CHANNEL, DB_PATH, login_required, cfg, BASE).
If those are not importable it falls back to internal equivalents so the module
is also runnable standalone for testing.

Required extra packages (pip install into the same venv):
    google-api-python-client google-auth google-auth-oauthlib Pillow

Google Cloud Console setup (for the user):
    1. https://console.cloud.google.com/  -> New Project
    2. APIs & Services -> Library -> "Google Drive API" -> Enable
    3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
       - Application type: Web application
       - Authorized redirect URIs:  http://<host>:8050/api/gdrive/callback
    4. Download JSON, copy the values into config.env:
         GDRIVE_CLIENT_ID=....apps.googleusercontent.com
         GDRIVE_CLIENT_SECRET=....
       (or set GOOGLE_CREDENTIALS_PATH=/path/to/client_secret.json)
    5. "OAuth consent screen" -> add your Google account as a Test User
       (while publishing status is "Testing").
"""

import os
import re
import io
import json
import sys
import sqlite3
import secrets
import threading
import time as _time
import mimetypes
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# Integration with the host web.py module (graceful fallback if not present)
# ----------------------------------------------------------------------------
try:
    from web import (
        app as _host_app,
        login_required,
        db_query, db_exec, db_scalar,
        run_async, telethon_client, DB_PATH, BASE, cfg,
    )
    _HAS_HOST = True
except Exception:  # pragma: no cover - standalone testing
    _HAS_HOST = False

# Local config / paths
if _HAS_HOST:
    BASE = BASE
    DB_PATH = DB_PATH
    cfg = cfg
    # Ensure cfg has Google keys (may not be loaded yet at import time)
    _env = os.path.join(BASE, "config.env")
    if os.path.exists(_env) and not cfg.get("GDRIVE_CLIENT_ID"):
        with open(_env) as _f:
            for _l in _f:
                _l = _l.strip()
                if not _l or _l.startswith("#") or "=" not in _l: continue
                _k, _v = _l.split("=", 1)
                cfg[_k.strip()] = _v.strip()
else:  # standalone fallback
    BASE = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE, "storage.db")
    cfg = {}

# Paths
UPLOADS_DIR = os.path.join(BASE, "uploads")
PROFILES_DIR = os.path.join(UPLOADS_DIR, "profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)

# ----------------------------------------------------------------------------
# Optional Google libs — import lazily so the module loads even if not installed
# ----------------------------------------------------------------------------
try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from google.auth.transport.requests import Request as GoogleRequest
    _HAS_GOOGLE = True
except Exception:  # pragma: no cover
    _HAS_GOOGLE = False

# Drive API scopes
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/photoslibrary.readonly", "https://www.googleapis.com/auth/photospicker.mediaitems.readonly", "openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]

# Channel target (use the configured one; fall back to "me" in tests)
CHANNEL = int(cfg.get("CHANNEL", "0"))

# ----------------------------------------------------------------------------
# Local DB helpers (only used if host helpers absent)
# ----------------------------------------------------------------------------
def _local_db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def _local_db_exec(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

def _local_db_scalar(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute(sql, params).fetchone()
    conn.close()
    return r[0] if r else None

if not _HAS_HOST:
    db_query = _local_db_query
    db_exec = _local_db_exec
    db_scalar = _local_db_scalar

# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------
def init_drive_db():
    """Create the extension tables. Safe to call repeatedly."""
    db_exec("""CREATE TABLE IF NOT EXISTS user_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT UNIQUE,
        name TEXT,
        email TEXT,
        bio TEXT,
        phone TEXT,
        photo_path TEXT,
        updated_at TEXT
    )""")
    db_exec("""CREATE TABLE IF NOT EXISTS google_tokens (
        user_id TEXT PRIMARY KEY,
        access_token TEXT,
        refresh_token TEXT,
        token_expiry TEXT
    )""")
    # OAuth state is persisted server-side because some mobile/in-app browsers
    # do not return the Flask session cookie after the Google cross-site redirect.
    db_exec("""CREATE TABLE IF NOT EXISTS google_oauth_states (
        state TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        flow_type TEXT NOT NULL DEFAULT 'drive',
        code_verifier TEXT,
        created_at INTEGER NOT NULL
    )""")
    db_exec("""CREATE TABLE IF NOT EXISTS sync_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drive_file_id TEXT UNIQUE NOT NULL,
        file_name TEXT NOT NULL,
        error_msg TEXT,
        folder_id INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
    )""")
    db_exec("DELETE FROM google_oauth_states WHERE created_at < ?", (int(_time.time()) - 900,))

# ----------------------------------------------------------------------------
# Google credentials loading
# ----------------------------------------------------------------------------
def _creds_path():
    return cfg.get("GOOGLE_CREDENTIALS_PATH") or os.path.join(BASE, "client_secret.json")

def _client_config():
    """Build the client config dict from config.env or a credentials JSON file."""
    _ensure_cfg_loaded()
    cid = cfg.get("GDRIVE_CLIENT_ID")
    csecret = cfg.get("GDRIVE_CLIENT_SECRET")
    if cid and csecret:
        return {
            "web": {
                "client_id": cid,
                "client_secret": csecret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [],
            }
        }
    p = _creds_path()
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _human_size(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _save_profile_photo(user_id, stream, original_name):
    """Save an uploaded image (validates/resizes via Pillow) under profiles dir.
    Returns the relative stored path or None on failure."""
    from PIL import Image
    try:
        img = Image.open(stream)
        img = img.convert("RGB")
        img.thumbnail((512, 512))
        ext = os.path.splitext(original_name)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_") or "user"
        fname = f"{safe}{ext}"
        dest = os.path.join(PROFILES_DIR, fname)
        img.save(dest, quality=85)
        return os.path.relpath(dest, BASE)
    except Exception as e:
        print(f"[profile] photo save failed: {e}")
        return None

# ----------------------------------------------------------------------------
# Blueprint
# ----------------------------------------------------------------------------
from flask import (
    Blueprint, request, jsonify, redirect, url_for, session, send_file, Response,
)

drive_ext = Blueprint("drive_ext", __name__, url_prefix="")

# ============================================================================
#  PROFILE FEATURES
# ============================================================================
@drive_ext.route("/api/profile", methods=["GET"])
def api_profile_get():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    row = db_query("SELECT * FROM user_profiles WHERE user_id=?", (uid,))
    if not row:
        return jsonify({"profile": None})
    r = row[0]
    return jsonify({
        "profile": {
            "user_id": r["user_id"],
            "name": r["name"] or "",
            "email": r["email"] or "",
            "bio": r["bio"] or "",
            "phone": r["phone"] or "",
            "photo_url": (url_for("drive_ext.api_profile_photo", user_id=r["user_id"])
                          if r["photo_path"] else None),
            "updated_at": r["updated_at"],
        }
    })

@drive_ext.route("/api/profile", methods=["POST"])
def api_profile_post():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    bio = (data.get("bio") or "").strip()
    phone = (data.get("phone") or "").strip()

    existing = db_query("SELECT id, photo_path FROM user_profiles WHERE user_id=?", (uid,))
    if existing:
        db_exec(
            """UPDATE user_profiles
               SET name=?, email=?, bio=?, phone=?, updated_at=?
               WHERE user_id=?""",
            (name, email, bio, phone, _now(), uid),
        )
    else:
        db_exec(
            """INSERT INTO user_profiles
               (user_id, name, email, bio, phone, photo_path, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, name, email, bio, phone, None, _now()),
        )
    return jsonify({"ok": True, "profile": {
        "user_id": uid, "name": name, "email": email,
        "bio": bio, "phone": phone,
    }})

@drive_ext.route("/api/profile/photo", methods=["POST"])
def api_profile_photo_post():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    f = request.files.get("photo")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    if not f.content_type or not f.content_type.startswith("image/"):
        return jsonify({"error": "file must be an image"}), 400
    rel = _save_profile_photo(uid, f.stream, f.filename)
    if not rel:
        return jsonify({"error": "failed to process image"}), 400
    db_exec(
        "INSERT INTO user_profiles (user_id, photo_path, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET photo_path=excluded.photo_path, updated_at=excluded.updated_at",
        (uid, rel, _now()),
    )
    return jsonify({"ok": True, "photo_url": url_for("drive_ext.api_profile_photo", user_id=uid)})

@drive_ext.route("/api/profile/photo/<user_id>")
def api_profile_photo(user_id):
    row = db_query("SELECT photo_path FROM user_profiles WHERE user_id=?", (user_id,))
    if not row or not row[0]["photo_path"]:
        return jsonify({"error": "no photo"}), 404
    path = os.path.join(BASE, row[0]["photo_path"])
    if not os.path.exists(path):
        return jsonify({"error": "no photo"}), 404
    return send_file(path)

@drive_ext.route("/profile")
def profile_page():
    if not session.get("user_id"):
        return redirect(url_for("login_page"))
    return PROFILE_HTML

# ============================================================================
#  GOOGLE DRIVE FEATURES
# ============================================================================
@drive_ext.route("/api/gdrive/status")
def gdrive_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    row = db_query(
        "SELECT access_token, token_expiry FROM google_tokens WHERE user_id=?", (uid,)
    )
    if not row:
        return jsonify({"connected": False})
    # Evaluate expiry if possible
    connected = True
    try:
        exp = datetime.fromisoformat(row[0]["token_expiry"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            connected = False
    except Exception:
        connected = True
    return jsonify({"connected": connected})

def _get_credentials(uid):
    """Load and refresh stored Google credentials for a user."""
    _ensure_cfg_loaded()
    if not _HAS_GOOGLE:
        return None
    row = db_query(
        "SELECT access_token, refresh_token, token_expiry FROM google_tokens WHERE user_id=?",
        (uid,),
    )
    if not row:
        return None
    r = row[0]
    creds = Credentials(
        token=r["access_token"],
        refresh_token=r["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cfg.get("GDRIVE_CLIENT_ID"),
        client_secret=cfg.get("GDRIVE_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            db_exec(
                "UPDATE google_tokens SET access_token=?, token_expiry=? WHERE user_id=?",
                (creds.token, creds.expiry.isoformat(), uid),
            )
        except Exception as e:
            print(f"[gdrive] token refresh failed: {e}")
            return None
    return creds

def _build_drive_service(uid):
    creds = _get_credentials(uid)
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)

def _ensure_cfg_loaded():
    """Force-load config.env if Google keys missing (import-time race fix)."""
    if cfg.get("GDRIVE_CLIENT_ID"):
        return
    _env = os.path.join(BASE, "config.env")
    if not os.path.exists(_env):
        return
    with open(_env) as _f:
        for _l in _f:
            _l = _l.strip()
            if not _l or _l.startswith("#") or "=" not in _l:
                continue
            _k, _v = _l.split("=", 1)
            cfg[_k.strip()] = _v.strip()

# ============================================================================
#  GOOGLE DRIVE FEATURES
# ============================================================================
@drive_ext.route("/api/gdrive/auth")
def gdrive_auth():
    _ensure_cfg_loaded()
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if not _HAS_GOOGLE:
        return jsonify({"error": "google libs not installed"}), 500
    client_config = _client_config()
    if not client_config:
        return jsonify({
            "error": "Google credentials not configured. "
                     "Set GDRIVE_CLIENT_ID/SECRET in config.env or provide client_secret.json"
        }), 500

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https")),
    )
    # Per-user state to bind callback to the right session user
    state = "drive:" + secrets.token_urlsafe(16)
    session["gdrive_state"] = state
    session["gdrive_uid"] = uid
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    verifier = flow.code_verifier if hasattr(flow, "code_verifier") else None
    db_exec("INSERT OR REPLACE INTO google_oauth_states (state,user_id,flow_type,code_verifier,created_at) VALUES (?,?,?,?,?)", (state, uid, "drive", verifier, int(_time.time())))
    if verifier:
        session["gdrive_code_verifier"] = verifier
    return redirect(auth_url)

@drive_ext.route("/api/gdrive/callback")
def gdrive_callback():
    incoming_state = request.args.get("state", "")
    # Detect flow type from state prefix — session may be lost behind reverse proxy
    # Handle Google LOGIN flow
    if incoming_state == "login" or session.get("login_flow"):
        _ensure_cfg_loaded()
        session.pop("login_flow", None)
        code = request.args.get("code")
        if not code:
            return redirect("/login?error=no_code")
        import requests as _req
        # Use only login scopes - NOT full Drive scopes
        _ensure_cfg_loaded()
        _cid = cfg.get("GDRIVE_CLIENT_ID", "")
        _csec = cfg.get("GDRIVE_CLIENT_SECRET", "")
        print("[GOOGLE_LOGIN] cfg loaded: cid=%s csec=%s" % (_cid[:20] if _cid else "EMPTY", _csec[:10] if _csec else "EMPTY"), file=sys.stderr, flush=True)
        token_resp = _req.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": _cid,
            "client_secret": _csec,
            "redirect_uri": url_for("drive_ext.gdrive_callback", _external=True, _scheme="https"),
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"})
        token_data = token_resp.json()
        print("[GOOGLE_LOGIN] Token response: status=%s keys=%s" % (token_resp.status_code, sorted(token_data.keys())), file=sys.stderr, flush=True)
        access_token = token_data.get("access_token")
        if not access_token:
            print("[GOOGLE_LOGIN] Token exchange failed: %s" % token_data, file=sys.stderr, flush=True)
            return redirect("/login?error=token_failed")
        user_resp = _req.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={
            "Authorization": "Bearer " + access_token,
        })
        user_info = user_resp.json()
        email = user_info.get("email", "").lower()
        name = user_info.get("name", "")
        picture = user_info.get("picture", "")
        # Check allowlist — import from web module
        import sys as _sys
        _web = _sys.modules.get("web")
        google_allowed = set(x.strip().lower() for x in (_web.cfg.get("GOOGLE_ALLOWED_EMAILS", "") if hasattr(_web, "cfg") else "").split(",") if x.strip()) if hasattr(_web, "cfg") else {"bowor4751@gmail.com"}
        allowed = set(x.strip() for x in (_web.cfg.get("ALLOWED_USERS", "") if hasattr(_web, "cfg") else "").split(",") if x.strip()) if hasattr(_web, "cfg") else set()
        if email not in google_allowed and email not in allowed:
            return redirect("/login?error=email_not_allowed&email=" + email)
        session["google_email"] = email
        session["google_name"] = name
        session["google_picture"] = picture
        session["user_id"] = email
        session.permanent = True
        return redirect("/drive")
    is_photos = incoming_state.startswith("photos:") or session.get("photos_uid")
    is_drive = incoming_state.startswith("drive:")
    if is_photos:
        photos_uid = session.get("photos_uid") or session.get("gdrive_uid")
        if not photos_uid:
            return jsonify({"error": "auth session expired, please login again and retry"}), 400
        return _photos_callback_handler(photos_uid)
    uid = session.get("gdrive_uid")
    expected_state = session.get("gdrive_state")
    saved_verifier = session.pop("gdrive_code_verifier", None)
    # Mobile/in-app browsers can lose the session cookie across Google's redirect.
    # Recover only through the exact, short-lived, single-use server-side state record.
    state_row = None
    if incoming_state:
        rows = db_query("SELECT user_id, code_verifier, created_at FROM google_oauth_states WHERE state=? AND flow_type='drive'", (incoming_state,))
        state_row = rows[0] if rows else None
    if (not uid or not expected_state) and state_row:
        created = state_row["created_at"] if hasattr(state_row, "keys") else state_row[2]
        if int(_time.time()) - int(created) <= 900:
            uid = state_row["user_id"] if hasattr(state_row, "keys") else state_row[0]
            saved_verifier = (state_row["code_verifier"] if hasattr(state_row, "keys") else state_row[1]) or saved_verifier
            expected_state = incoming_state
    if not uid or not expected_state:
        return redirect("/drive#gdrive-auth-expired")
    if incoming_state != expected_state:
        return jsonify({"error": "state mismatch (CSRF)"}), 400

    client_config = _client_config()
    if not client_config:
        return jsonify({"error": "Google credentials not configured"}), 500

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https")),
        state=expected_state,
    )
    if saved_verifier:
        flow.code_verifier = saved_verifier
    try:
        _auth_resp = request.url.replace("http://", "https://", 1)
        flow.fetch_token(authorization_response=_auth_resp)
    except Exception as e:
        return jsonify({"error": f"token exchange failed: {e}"}), 400

    creds = flow.credentials
    expiry = creds.expiry.isoformat() if creds.expiry else None
    db_exec(
        """INSERT INTO google_tokens (user_id, access_token, refresh_token, token_expiry)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             access_token=excluded.access_token,
             refresh_token=COALESCE(excluded.refresh_token, google_tokens.refresh_token),
             token_expiry=excluded.token_expiry""",
        (uid, creds.token, creds.refresh_token, expiry),
    )
    session.pop("gdrive_state", None)
    session.pop("gdrive_uid", None)
    db_exec("DELETE FROM google_oauth_states WHERE state=?", (incoming_state,))
    # Restore the authenticated app session if the mobile browser dropped its cookie.
    session["user_id"] = uid
    session.permanent = True
    # Redirect back to the embedded Google Drive panel inside /drive
    return redirect("/drive#gdrive")

@drive_ext.route("/api/gdrive/files")
def gdrive_files():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    folder_id = request.args.get("folder_id") or "root"
    service = _build_drive_service(uid)
    if not service:
        return jsonify({"error": "not connected to Google Drive", "connected": False}), 401

    try:
        query = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(
            q=query,
            pageSize=100,
            fields="files(id, name, mimeType, size, modifiedTime, parents)",
            orderBy="folder, name",
        ).execute()
        items = results.get("files", [])
    except HttpError as e:
        return jsonify({"error": f"Drive API error: {e}"}), 502

    files = []
    folders = []
    for it in items:
        is_folder = it.get("mimeType") == "application/vnd.google-apps.folder"
        entry = {
            "id": it["id"],
            "name": it.get("name", "(untitled)"),
            "mimeType": it.get("mimeType", ""),
            "size": int(it.get("size", 0) or 0),
            "size_human": _human_size(int(it.get("size", 0) or 0)),
            "modified": it.get("modifiedTime", ""),
            "is_folder": is_folder,
            "is_google_doc": it.get("mimeType", "").startswith("application/vnd.google-apps"),
        }
        if is_folder:
            folders.append(entry)
        else:
            files.append(entry)

    return jsonify({"folder_id": folder_id, "folders": folders, "files": files})

@drive_ext.route("/api/gdrive/disconnect", methods=["POST"])
def gdrive_disconnect():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    db_exec("DELETE FROM google_tokens WHERE user_id=?", (uid,))
    return jsonify({"ok": True, "connected": False})

# Sync state (must be initialized before route handlers)
_sync_state = {
    "running": False, "uid": "", "total": 0, "done": 0, "errors": [],
    "imported": 0, "message": "", "files": [], "updated_at": 0,
    "pause_event": threading.Event(), "cancel_flag": False, "current": ""
}
_sync_state["pause_event"].set()

@drive_ext.route("/api/gdrive/sync", methods=["GET"])
def gdrive_sync_start():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if _sync_state["running"]:
        return jsonify({"ok": True, "status": "running", "message": "Sync sedang berjalan..."})
    t = threading.Thread(target=_do_sync, args=(uid,), daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "started"})

@drive_ext.route("/api/gdrive/sync/status")
def gdrive_sync_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    # Auto-detect stuck sync (no update for >10 min)
    if _sync_state["running"] and _sync_state["updated_at"] > 0:
        if (_time.time() - _sync_state["updated_at"]) > 600:
            print("[SYNC] Detected stuck sync, auto-resetting", flush=True)
            _sync_state["running"] = False
            _sync_state["message"] = "Sync terhenti (timeout). Silakan coba lagi."
    pct = 0
    if _sync_state["total"] > 0:
        pct = int((_sync_state["done"] * 100) / _sync_state["total"])
    storage_total = db_scalar("SELECT COUNT(*) FROM files") or 0
    return jsonify({
        "running": _sync_state["running"],
        "storage_total": storage_total,
        "total": _sync_state["total"],
        "done": _sync_state["done"],
        "imported": _sync_state["imported"],
        "errors_count": len(_sync_state["errors"]),
        "errors": _sync_state["errors"][:10],
        "message": _sync_state["message"],
        "percent": pct,
        "files": _sync_state["files"][-20:],
        "paused": (not _sync_state["pause_event"].is_set()) if _sync_state["running"] else False,
        "current": _sync_state.get("current", "")
    })

@drive_ext.route("/api/gdrive/sync/pause")
def gdrive_sync_pause():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if not _sync_state["running"]:
        return jsonify({"ok": False, "message": "Sync not running"})
    _sync_state["pause_event"].clear()
    _sync_state["message"] = "Paused"
    _sync_state["updated_at"] = _time.time()
    print("[SYNC] Paused by user", file=sys.stderr, flush=True)
    return jsonify({"ok": True, "paused": True})

@drive_ext.route("/api/gdrive/sync/resume")
def gdrive_sync_resume():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if not _sync_state["running"]:
        return jsonify({"ok": False, "message": "Sync not running"})
    _sync_state["pause_event"].set()
    _sync_state["message"] = "Resumed"
    _sync_state["updated_at"] = _time.time()
    print("[SYNC] Resumed by user", file=sys.stderr, flush=True)
    return jsonify({"ok": True, "paused": False})

@drive_ext.route("/api/gdrive/sync/cancel")
def gdrive_sync_cancel():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if not _sync_state["running"]:
        return jsonify({"ok": False, "message": "Sync not running"})
    _sync_state["cancel_flag"] = True
    _sync_state["pause_event"].set()
    _sync_state["message"] = "Cancelling..."
    _sync_state["updated_at"] = _time.time()
    return jsonify({"ok": True, "cancelled": True})

@drive_ext.route("/api/gdrive/sync/retry")
def gdrive_sync_retry():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if _sync_state["running"]:
        return jsonify({"ok": False, "message": "Sync sedang berjalan"})
    # Get unresolved errors
    errors = db_query("SELECT drive_file_id, file_name, folder_id FROM sync_errors WHERE resolved=0")
    if not errors:
        return jsonify({"ok": False, "message": "Tidak ada file gagal"})
    t = threading.Thread(target=_do_retry, args=(uid, errors), daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "started", "count": len(errors)})

def _do_sync(uid):
    """Sync all Google Drive files to Telegram storage."""
    global _sync_state
    _sync_state = {
        "running": True, "uid": uid, "total": 0, "done": 0, "errors": [],
        "imported": 0, "message": "Connecting to Google Drive...", "files": [],
        "updated_at": _time.time(), "pause_event": threading.Event(),
        "cancel_flag": False, "current": ""
    }
    _sync_state["pause_event"].set()
    try:
        import sys, io, os, tempfile, hashlib
        from googleapiclient.http import MediaIoBaseDownload

        print("[SYNC] Building Drive service for uid=%s..." % uid, file=sys.stderr, flush=True)
        service = _build_drive_service(uid)
        if not service:
            _sync_state["message"] = "Google Drive not connected"
            _sync_state["running"] = False
            print("[SYNC] Drive service is None - not connected", file=sys.stderr, flush=True)
            return
        print("[SYNC] Drive service built OK", file=sys.stderr, flush=True)

        # telethon_client was still None when this extension imported it. Resolve
        # the live client from the already-running __main__ module without
        # importing/executing web.py again.
        client = globals().get("telethon_client")
        _run_async_fn = globals().get("run_async")
        host_channel = CHANNEL
        try:
            import __main__ as _host
            client = getattr(_host, "telethon_client", client)
            _run_async_fn = getattr(_host, "run_async", _run_async_fn)
            # web.py is executed as __main__; its config is authoritative.
            host_channel = getattr(_host, "CHANNEL", host_channel)
        except Exception:
            pass
        if not client or not _run_async_fn:
            _sync_state["message"] = "Telegram client not ready"
            _sync_state["running"] = False
            return
        print("[SYNC] Using existing Telegram client: %s" % type(client).__name__, file=sys.stderr, flush=True)

        # Resolve Telegram channel entity ONCE before the loop.
        target = int(host_channel)
        if target <= 0:
            _sync_state["message"] = "Telegram channel belum dikonfigurasi"
            _sync_state["running"] = False
            print("[SYNC] FATAL: invalid host CHANNEL=%r" % (host_channel,), file=sys.stderr, flush=True)
            return
        if target > 0:
            target = -int("100" + str(target))
        try:
            entity = _run_async_fn(client.get_entity(target))
            print("[SYNC] Resolved entity: %s id=%s title=%s" % (type(entity).__name__, getattr(entity, 'id', '?'), getattr(entity, 'title', '?')), file=sys.stderr, flush=True)
        except Exception as ent_err:
            _sync_state["message"] = "Telegram channel error: %s" % str(ent_err)[:100]
            _sync_state["running"] = False
            print("[SYNC] FATAL: get_entity(%s) failed: %s" % (target, ent_err), file=sys.stderr, flush=True)
            return

        # List all files from Google Drive (paginated)
        _sync_state["message"] = "Listing Google Drive files..."
        all_files = []
        page_token = None
        print("[SYNC] Listing Drive files...", file=sys.stderr, flush=True)
        while True:
            resp = service.files().list(
                q="trashed=false",
                fields="nextPageToken, files(id, name, mimeType, size, parents)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # Filter to files only (skip folders)
        drive_files = [f for f in all_files if not f.get("mimeType", "").endswith(".folder")]
        print("[SYNC] Listed %d total files, %d non-folder" % (len(all_files), len(drive_files)), file=sys.stderr, flush=True)
        print("[SYNC] Found %d files on Google Drive" % len(drive_files), file=sys.stderr, flush=True)

        # Every account gets a dedicated backup root; never write sync results to Home.
        safe_email = str(uid).strip().lower().replace("/", "_").replace("\\", "_")
        backup_name = "Backup - " + safe_email
        db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, 0)", (backup_name,))
        backup_rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=0", (backup_name,))
        if not backup_rows:
            raise RuntimeError("Backup folder could not be created")
        backup_folder_id = backup_rows[0]["id"] if hasattr(backup_rows[0], "keys") else backup_rows[0][0]
        print("[SYNC] Backup root: %s (folder_id=%s)" % (backup_name, backup_folder_id), file=sys.stderr, flush=True)

        # Build folder mapping (Drive folder ID -> DB folder ID) under this backup root.
        folder_by_id = {}
        drive_folders = [f for f in all_files if f.get("mimeType", "").endswith(".folder")]
        db_folders = db_query("SELECT id, name FROM folders WHERE parent_id=?", (backup_folder_id,))
        db_name_map = {(r["name"] if hasattr(r, "keys") else r[1]): (r["id"] if hasattr(r, "keys") else r[0]) for r in db_folders}

        for ff in drive_folders:
            fname = ff.get("name", "")
            if fname in db_name_map:
                folder_by_id[ff["id"]] = db_name_map[fname]

        def _get_db_folder(drive_folder_id):
            if drive_folder_id in folder_by_id:
                return folder_by_id[drive_folder_id]
            # Try to find/create by name
            for ff in drive_folders:
                if ff["id"] == drive_folder_id:
                    fname = ff.get("name", "Unknown")
                    db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)", (fname, backup_folder_id))
                    row = db_query("SELECT id FROM folders WHERE name=? AND parent_id=?", (fname, backup_folder_id))
                    if row:
                        fid = row[0]["id"] if hasattr(row[0], "keys") else row[0][0]
                        folder_by_id[drive_folder_id] = fid
                        return fid
            return backup_folder_id

        # Find files already in DB (by file_name)
        existing_names = set()
        for r in db_query("SELECT file_name FROM files"):
            existing_names.add(r["file_name"] if hasattr(r, "keys") else r[0])

        missing = [f for f in drive_files if f.get("name", "") not in existing_names]
        print("[SYNC] %d files to sync (%d already in DB)" % (len(missing), len(drive_files) - len(missing)), file=sys.stderr, flush=True)
        print("[SYNC] %d files to sync (%d already in DB)" % (len(missing), len(drive_files) - len(missing)), file=sys.stderr, flush=True)

        _sync_state["total"] = len(missing)
        _sync_state["message"] = "Syncing %d files..." % len(missing)

        if not missing:
            _sync_state["message"] = "All files already synced!"
            _sync_state["running"] = False
            return

        for i, f in enumerate(missing):
            # --- Pause / Cancel controls ---
            if _sync_state["cancel_flag"]:
                print("[SYNC] Cancel flag set, stopping at %d/%d" % (i, len(missing)), file=sys.stderr, flush=True)
                _sync_state["message"] = "Cancelled at %d/%d (%d imported, %d errors)" % (
                    i, len(missing), _sync_state["imported"], len(_sync_state["errors"]))
                _sync_state["running"] = False
                return
            _sync_state["pause_event"].wait()

            try:
                name = f.get("name", "unknown")
                _sync_state["current"] = name
                _sync_state["message"] = "[%d/%d] %s" % (i+1, len(missing), name)
                _sync_state["done"] = i
                _sync_state["updated_at"] = _time.time()
                print("[SYNC] Processing %d/%d: %s" % (i+1, len(missing), name), file=sys.stderr, flush=True)

                folder_id = backup_folder_id
                pr = f.get("parents") or []
                if pr:
                    try:
                        folder_id = _get_db_folder(pr[0])
                    except Exception as fe:
                        print("[SYNC] folder resolve failed: %s" % fe, file=sys.stderr, flush=True)
                        folder_id = backup_folder_id

                mime = f.get("mimeType", "")
                if mime.startswith("application/vnd.google-apps."):
                    export_map = {
                        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
                        "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
                        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
                        "application/vnd.google-apps.script": ("text/plain", ".txt"),
                    }
                    exp_mime, exp_ext = export_map.get(mime, ("application/pdf", ".pdf"))
                    req_dl = service.files().export_media(fileId=f["id"], mimeType=exp_mime)
                    if not os.path.splitext(name)[1]:
                        name = name + exp_ext
                else:
                    req_dl = service.files().get_media(fileId=f["id"])
                fh = io.BytesIO()
                dl = MediaIoBaseDownload(fh, req_dl)
                done_chunk = False
                while not done_chunk:
                    status, done_chunk = dl.next_chunk()
                fh.seek(0)

                ext = os.path.splitext(name)[1] or ".bin"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(fh.read())
                tmp.close()

                size = os.path.getsize(tmp.name)
                fh_hash = hashlib.md5(name.encode()).hexdigest()[:12]

                # Reuse the entity resolved once before the loop.
                msg = _run_async_fn(client.send_file(entity, tmp.name, caption="cloud:%d:%s" % (folder_id, name), force_document=True))
                os.unlink(tmp.name)
                msg_id = getattr(msg, "id", None)
                db_exec(
                    "INSERT INTO files (file_name, msg_id, size, mime, file_hash, folder_id) VALUES (?,?,?,?,?,?)",
                    (name, msg_id, size, f.get("mimeType", "") or "application/octet-stream", fh_hash, folder_id)
                )
                _sync_state["imported"] += 1
                _sync_state["done"] = i + 1
                _sync_state["updated_at"] = _time.time()
                _sync_state["files"].append({"name": name, "ok": True})
                print("[SYNC] Uploaded %s -> folder_id=%s (%d/%d)" % (name, folder_id, i+1, len(missing)), file=sys.stderr, flush=True)

            except Exception as e:
                _err_name = f.get("name", "?")
                _err_drive_id = f.get("id", "")
                print("[SYNC] ERROR on %s: %s" % (_err_name, e), file=sys.stderr, flush=True)
                _sync_state["errors"].append("%s: %s" % (_err_name, str(e)[:80]))
                _sync_state["files"].append({"name": _err_name, "ok": False, "error": str(e)[:60]})
                try:
                    db_exec(
                        "INSERT INTO sync_errors (drive_file_id, file_name, error_msg, folder_id) VALUES (?,?,?,?)",
                        (_err_drive_id, _err_name, str(e)[:200], folder_id)
                    )
                except Exception:
                    pass

        _sync_state["done"] = len(missing)
        _sync_state["message"] = "Done! %d imported, %d errors" % (_sync_state["imported"], len(_sync_state["errors"]))
        _sync_state["current"] = ""
        _sync_state["running"] = False

    except Exception as e:
        import traceback
        print("[SYNC] FATAL: %s" % e, file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        _sync_state["message"] = "Error: %s" % str(e)
        _sync_state["running"] = False

def _do_retry(uid, errors):
    global _sync_state
    _sync_state = {
        "running": True, "uid": uid, "total": len(errors), "done": 0, "errors": [],
        "imported": 0, "message": "Retrying failed files...", "files": [], "updated_at": _time.time(),
        "pause_event": threading.Event(), "cancel_flag": False, "current": ""
    }
    _sync_state["pause_event"].set()
    try:
        import sys, io, os, tempfile, hashlib
        from googleapiclient.http import MediaIoBaseDownload

        service = _build_drive_service(uid)
        if not service:
            _sync_state["message"] = "Google Drive not connected"
            _sync_state["running"] = False
            return

        target = int(CHANNEL)
        if target > 0:
            target = -int("100" + str(target))

        # Reuse existing telethon_client + run_async from web.py
        client = globals().get("telethon_client")
        _run_async_fn = globals().get("run_async")
        if not client or not _run_async_fn:
            _sync_state["message"] = "Telegram client not ready"
            _sync_state["running"] = False
            return
        print("[SYNC] Using existing Telegram client: %s" % type(client).__name__, file=sys.stderr, flush=True)

        # Normalize persisted failed rows into the same shape used by the sync loop.
        missing = [{
            "id": (r["drive_file_id"] if hasattr(r, "keys") else r[0]),
            "name": (r["file_name"] if hasattr(r, "keys") else r[1]),
            "folder_id": (r["folder_id"] if hasattr(r, "keys") else r[2]),
        } for r in errors]

        for i, f in enumerate(missing):
                # --- Pause / Cancel controls ---
                if _sync_state["cancel_flag"]:
                    print("[SYNC] Cancel flag set, stopping at %d/%d" % (i, len(missing)), file=sys.stderr, flush=True)
                    _sync_state["message"] = "Cancelled at %d/%d (%d imported, %d errors)" % (
                        i, len(missing), _sync_state["imported"], len(_sync_state["errors"]))
                    _sync_state["running"] = False
                    return
                # Blocks while paused (pause_event cleared)
                _sync_state["pause_event"].wait()

                try:
                    name = f["name"]
                    _sync_state["current"] = name
                    _sync_state["message"] = "[%d/%d] %s" % (i+1, len(missing), name)
                    _sync_state["done"] = i
                    _sync_state["updated_at"] = _time.time()
                    print("[SYNC] Processing %d/%d: %s" % (i+1, len(missing), name), file=sys.stderr, flush=True)

                    # Retry into the same Cloud Storage folder recorded on failure.
                    folder_id = int(f.get("folder_id") or 0)

                    # Google Docs/Sheets/Slides need export, not download
                    mime = f.get("mimeType", "")
                    if mime.startswith("application/vnd.google-apps."):
                        export_map = {
                            "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
                            "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
                            "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
                            "application/vnd.google-apps.script": ("text/plain", ".txt"),
                        }
                        exp_mime, exp_ext = export_map.get(mime, ("application/pdf", ".pdf"))
                        req_dl = service.files().export_media(fileId=f["id"], mimeType=exp_mime)
                        if not os.path.splitext(name)[1]:
                            name = name + exp_ext
                    else:
                        req_dl = service.files().get_media(fileId=f["id"])
                    fh = io.BytesIO()
                    dl = MediaIoBaseDownload(fh, req_dl)
                    done_chunk = False
                    while not done_chunk:
                        status, done_chunk = dl.next_chunk()
                    fh.seek(0)

                    ext = os.path.splitext(name)[1] or ".bin"
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                    tmp.write(fh.read())
                    tmp.close()

                    size = os.path.getsize(tmp.name)
                    fh_hash = __import__("hashlib").md5(name.encode()).hexdigest()[:12]

                    msg = _run_async_fn(client.send_file(int(target), tmp.name, caption="cloud:%d:%s" % (folder_id, name), force_document=True))
                    os.unlink(tmp.name)
                    msg_id = getattr(msg, "id", None)
                    # Insert into DB files table with correct folder_id
                    db_exec(
                        "INSERT INTO files (file_name, msg_id, size, mime, file_hash, folder_id) VALUES (?,?,?,?,?,?)",
                        (name, msg_id, size, f.get("mimeType", "") or "application/octet-stream", fh_hash, folder_id)
                    )
                    _sync_state["imported"] += 1
                    _sync_state["done"] = i + 1
                    _sync_state["updated_at"] = _time.time()
                    _sync_state["files"].append({"name": name, "ok": True})
                    db_exec("DELETE FROM sync_errors WHERE drive_file_id=?", (f.get("id", ""),))
                    print("[SYNC] Uploaded %s -> folder_id=%s (%d/%d)" % (name, folder_id, i+1, len(missing)), file=sys.stderr, flush=True)

                except Exception as e:
                    # Skip failed files, continue
                    print("[SYNC] ERROR on %s: %s" % (f.get("name", "?"), e), file=sys.stderr, flush=True)
                    _sync_state["errors"].append("%s: %s" % (f.get("name", "?"), str(e)[:80]))
                    _sync_state["files"].append({"name": f.get("name", "?"), "ok": False, "error": str(e)[:60]})

        # Close the event loop

        _sync_state["done"] = len(missing)
        _sync_state["message"] = "Done! %d imported, %d errors" % (_sync_state["imported"], len(_sync_state["errors"]))
        _sync_state["current"] = ""
        _sync_state["running"] = False

    except Exception as e:

        _sync_state["message"] = "Error: %s" % str(e)
        _sync_state["running"] = False

@drive_ext.route("/api/gdrive/copy", methods=["POST"])
def gdrive_copy():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True) or {}
    file_ids = data.get("file_ids") or []
    folder_id = int(data.get("folder_id", 0))
    if not file_ids:
        return jsonify({"error": "no files selected"}), 400

    service = _build_drive_service(uid)
    if not service:
        return jsonify({"error": "not connected to Google Drive", "connected": False}), 401

    # Ensure target channel resolved
    target = CHANNEL
    try:
        from web import CHANNEL as _cfg_channel
        if _cfg_channel and str(_cfg_channel) not in ("", "me", "__AUTO_CREATE__"):
            target = _cfg_channel
    except Exception:
        pass

    results = []
    for fid in file_ids:
        try:
            meta = service.files().get(fileId=fid, fields="name, mimeType, size").execute()
            name = meta.get("name", "file")
            mime = meta.get("mimeType", "")
            # Google Workspace docs must be exported
            if mime.startswith("application/vnd.google-apps"):
                if "document" in mime:
                    dl = service.files().export_media(fileId=fid, mimeType="application/pdf")
                    name = name + ".pdf"
                elif "spreadsheet" in mime:
                    dl = service.files().export_media(fileId=fid, mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    name = name + ".xlsx"
                elif "presentation" in mime:
                    dl = service.files().export_media(fileId=fid, mimeType="application/pdf")
                    name = name + ".pdf"
                else:
                    dl = service.files().get_media(fileId=fid)
            else:
                dl = service.files().get_media(fileId=fid)

            buf = io.BytesIO()
            downloader = dl  # googleapiclient media runs via .execute() streaming
            # Stream download
            import googleapiclient.http as ghttp
            if isinstance(dl, ghttp.MediaIoBaseDownload) is False and hasattr(dl, "execute"):
                data_bytes = dl.execute()
                buf.write(data_bytes)
            buf.seek(0)

            size = buf.getbuffer().nbytes
            fh = __import__("hashlib").md5(name.encode()).hexdigest()[:12]

            forwarded = run_async(
                telethon_client.send_file(
                    target,
                    file=buf,
                    caption=f"cloud:{folder_id}:{name}",
                    force_document=True,
                )
            )
            msg_id = forwarded.id
            if _HAS_HOST:
                db_exec(
                    "INSERT INTO files (file_name, msg_id, size, mime, file_hash, folder_id) VALUES (?,?,?,?,?,?)",
                    (name, msg_id, size, mime or "application/octet-stream", fh, folder_id),
                )
            results.append({"id": fid, "name": name, "size": size, "size_human": _human_size(size), "ok": True})
        except HttpError as e:
            results.append({"id": fid, "error": f"Drive API: {e}", "ok": False})
        except Exception as e:
            results.append({"id": fid, "error": str(e), "ok": False})

    return jsonify({"results": results})

@drive_ext.route("/gdrive")
def gdrive_page():
    if not session.get("user_id"):
        return redirect(url_for("login_page"))
    return GDRIVE_HTML

@drive_ext.route("/photos")
def photos_page():
    if not session.get("user_id"):
        return redirect(url_for("login_page"))
    return PHOTOS_HTML

# Backward-compatible alias for older dashboard links/bookmarks.
@drive_ext.route("/gphotos")
def photos_page_legacy():
    return redirect(url_for("drive_ext.photos_page"))

# ----------------------------------------------------------------------------
# HTML TEMPLATES
# ----------------------------------------------------------------------------
PROFILE_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage — Profile</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d0221;--card:#1a0a3e;--border:#6c3baa;--accent:#a78bfa;--accent2:#8b5cf6;--text:#e0e0e0;--muted:#6b7280;--danger:#f87171;--green:#34d399}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:#1a0a3e;border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent)}
.topbar .right{display:flex;align-items:center;gap:12px}
.topbar .right button{padding:6px 14px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}
.wrap{max-width:720px;margin:32px auto;padding:0 24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px}
.avatar-row{display:flex;align-items:center;gap:20px;margin-bottom:28px}
.avatar{width:96px;height:96px;border-radius:50%;background:#0d0221;border:2px solid var(--border);object-fit:cover;display:flex;align-items:center;justify-content:center;font-size:36px;color:var(--accent);overflow:hidden;flex-shrink:0}
.avatar img{width:100%;height:100%;object-fit:cover}
.avatar-actions{display:flex;flex-direction:column;gap:8px}
.btn-sm{padding:8px 16px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}
.btn-sm:hover{background:rgba(167,139,250,.1)}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:13px;color:var(--accent);margin-bottom:6px;font-weight:500}
.form-group input,.form-group textarea{width:100%;padding:12px 14px;background:#0d0221;border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;transition:.2s;font-family:inherit}
.form-group input:focus,.form-group textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(167,139,250,.2)}
.form-group textarea{resize:vertical;min-height:80px}
.btn-save{width:100%;padding:14px;background:linear-gradient(135deg,#6c3baa,#a78bfa);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:.2s;margin-top:8px}
.btn-save:hover{opacity:.9}
.btn-save:disabled{opacity:.5;cursor:not-allowed}
.msg{font-size:13px;margin-top:12px;text-align:center;min-height:18px}
.msg.ok{color:var(--green)}
.msg.err{color:var(--danger)}
.back{display:inline-block;margin-top:16px;color:var(--accent);text-decoration:none;font-size:13px}
.hidden{display:none}
</style>
</head>
<body>
<div class="topbar">
  <h1>☁️ Cloud Storage</h1>
  <div class="right">
    <button onclick="doLogout()">Logout</button>
  </div>
</div>
<div class="wrap">
  <div class="card">
    <div class="avatar-row">
      <div class="avatar" id="avatar">👤</div>
      <div class="avatar-actions">
        <button class="btn-sm" onclick="document.getElementById('photoInput').click()">📷 Ganti Foto</button>
        <button class="btn-sm" id="removePhoto" style="display:none" onclick="removePhoto()">🗑 Hapus Foto</button>
        <input type="file" id="photoInput" accept="image/*" hidden onchange="uploadPhoto()">
      </div>
    </div>
    <form id="profileForm" onsubmit="saveProfile(event)">
      <div class="form-group">
        <label>Nama</label>
        <input type="text" id="name" placeholder="Nama lengkap" maxlength="100">
      </div>
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="email" placeholder="email@example.com" maxlength="150">
      </div>
      <div class="form-group">
        <label>Telepon</label>
        <input type="text" id="phone" placeholder="+62..." maxlength="30">
      </div>
      <div class="form-group">
        <label>Bio</label>
        <textarea id="bio" placeholder="Tentang anda..." maxlength="500"></textarea>
      </div>
      <button type="submit" class="btn-save" id="saveBtn">💾 Simpan Profil</button>
    </form>
    <div class="msg" id="msg"></div>
  </div>
  <a class="back" href="/drive">← Kembali ke Drive</a>
<a class="back" href="/settings" style="border-color:#f59e0b;color:#f59e0b">⚙️ Pengaturan</a>
</div>
<script>
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());
async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  loadProfile();
}
async function loadProfile(){
  const d=await api('/api/profile');
  const p=d.profile;
  if(!p)return;
  document.getElementById('name').value=p.name||'';
  document.getElementById('email').value=p.email||'';
  document.getElementById('phone').value=p.phone||'';
  document.getElementById('bio').value=p.bio||'';
  if(p.photo_url){
    document.getElementById('avatar').innerHTML='<img src="'+p.photo_url+'">';
    document.getElementById('removePhoto').style.display='block';
  }
}
async function saveProfile(e){
  e.preventDefault();
  const btn=document.getElementById('saveBtn');
  btn.disabled=true;
  const msg=document.getElementById('msg');
  msg.textContent='';msg.className='msg';
  const d=await api('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:document.getElementById('name').value,email:document.getElementById('email').value,phone:document.getElementById('phone').value,bio:document.getElementById('bio').value})});
  if(d.ok){msg.textContent='✅ Profil tersimpan';msg.className='msg ok';}
  else{msg.textContent='❌ '+(d.error||'Gagal');msg.className='msg err';}
  btn.disabled=false;
}
async function uploadPhoto(){
  const f=document.getElementById('photoInput').files[0];
  if(!f)return;
  const fd=new FormData();fd.append('photo',f);
  const msg=document.getElementById('msg');msg.textContent='';msg.className='msg';
  const r=await fetch('/api/profile/photo',{method:'POST',body:fd,credentials:'same-origin'});
  const d=await r.json();
  if(d.ok){document.getElementById('avatar').innerHTML='<img src="'+d.photo_url+'">';document.getElementById('removePhoto').style.display='block';msg.textContent='✅ Foto diperbarui';msg.className='msg ok';}
  else{msg.textContent='❌ '+(d.error||'Gagal');msg.className='msg err';}
}
async function removePhoto(){
  // reset to default by removing local reference (soft - just hide)
  const msg=document.getElementById('msg');
  document.getElementById('avatar').innerHTML='👤';
  document.getElementById('removePhoto').style.display='none';
  msg.textContent='ℹ️ Foto dihapus dari tampilan (reload untuk mengembalikan)';msg.className='msg ok';
}
async function doLogout(){await api('/api/logout',{method:'POST'});location.href='/';}
async function retrySync(){
  if(!confirm('Retry file yang gagal?'))return;
  const btn=document.getElementById('retryBtn');
  btn.disabled=true;btn.textContent='Retrying...';
  try{
    const d=await api('/api/gdrive/sync/retry?t='+Date.now());
    if(d.ok){pollSync();}
    else{btn.textContent='Tidak ada file gagal';setTimeout(()=>{btn.style.display='none';},2000);}
  }catch(e){btn.textContent='Error: '+e;}
}
init();</script>
</body>
</html>'''

GDRIVE_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage — Google Drive</title>
<style>
*{box-sizing:border-box;margin:0;padding:0} :root{--bg:#090b15;--panel:#111424;--panel2:#161a2d;--line:#282b48;--purple:#7c3aed;--purple2:#9b6cff;--txt:#f7f7ff;--muted:#9ca3af;--green:#10b981;--yellow:#fbbf24;--red:#fb7185} body{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}.layout{display:flex;min-height:100vh}.app-sidebar{width:244px;flex:none;background:#0c0f1d;border-right:1px solid var(--line);padding:25px 15px;display:flex;flex-direction:column;position:fixed;inset:0 auto 0 0}.brand{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:800;padding:0 10px 27px}.cloud-logo{color:#c5a7ff;font-size:27px}.nav{display:grid;gap:5px}.nav a{color:#b5b9cf;text-decoration:none;padding:12px;border-radius:10px;font-size:14px;display:flex;gap:12px;align-items:center}.nav a:hover,.nav a.active{background:linear-gradient(90deg,rgba(124,58,237,.28),rgba(124,58,237,.05));color:white}.section-label{font-size:11px;color:#6f758f;font-weight:700;margin:25px 12px 9px;letter-spacing:.08em}.storage-card{display:flex;gap:10px;padding:13px 12px;border:1px solid #4e2c8d;border-radius:12px;background:linear-gradient(135deg,#25124d,#12172c);font-size:13px}.storage-card b{display:block}.storage-card small{display:block;color:var(--muted);margin-top:3px}.infinity{color:#b992ff;font-size:24px}.theme{display:flex;justify-content:space-between;align-items:center;padding:23px 12px;color:var(--muted);font-size:13px}.switch{width:36px;height:20px;border-radius:100px;background:var(--purple);padding:3px}.switch i{display:block;margin-left:16px;width:14px;height:14px;border-radius:50%;background:white}.service-nav{margin-top:auto;display:grid;gap:7px}.service-nav a{padding:12px;border-radius:10px;text-decoration:none;color:#aeb3c9;font-size:14px}.service-nav a.active{color:#fff;background:linear-gradient(120deg,#7c3aed,#5531bb);box-shadow:0 5px 20px rgba(124,58,237,.25)}.main{margin-left:244px;width:calc(100% - 244px);padding:18px 28px 35px}.top{height:48px;display:flex;gap:14px;align-items:center;margin-bottom:24px}.search{width:min(520px,48vw);position:relative}.search input{width:100%;background:#111424;border:1px solid var(--line);border-radius:10px;padding:12px 60px 12px 16px;color:#fff;outline:none}.search kbd{position:absolute;right:10px;top:10px;color:#81869e;border:1px solid #30354d;border-radius:5px;padding:2px 5px;font-size:10px}.user{margin-left:auto;color:#b3b7ca;font-size:13px}.quick{display:flex;gap:6px}.quick a,.top button{color:#c7cad8;background:#111424;border:1px solid var(--line);padding:9px 11px;border-radius:9px;font-size:12px;text-decoration:none;cursor:pointer}.quick a.active{border-color:#7352c6;color:#fff}.drive-hero{background:linear-gradient(125deg,#15182b,#101323);border:1px solid var(--line);box-shadow:0 0 30px rgba(79,70,229,.08);border-radius:17px;padding:25px}.hero-head{display:flex;justify-content:space-between;align-items:center;gap:18px}.drive-title{display:flex;gap:14px;align-items:center}.drive-mark{font-size:37px}.drive-title h1{font-size:25px}.drive-title p{font-size:13px;color:var(--muted);margin-top:5px}.connection{display:flex;align-items:center;gap:10px}.status-pill{color:#a7f3d0;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.3);border-radius:100px;padding:9px 13px;font-size:12px}.status-dot{width:8px;height:8px;display:inline-block;border-radius:50%;background:var(--red);margin-right:7px}.status-dot.on{background:var(--green);box-shadow:0 0 10px var(--green)}.btn{border:0;border-radius:9px;padding:10px 14px;background:linear-gradient(135deg,#7c3aed,#6035c9);color:#fff;font-weight:650;cursor:pointer;white-space:nowrap}.btn:disabled{opacity:.5;cursor:not-allowed}.btn.ghost{background:transparent;border:1px solid #8855ea;color:#cfb9ff}.browser{margin-top:30px}.browser h2{font-size:16px;margin-bottom:13px}.breadcrumb{font-size:13px;color:#aeb3c9;margin-bottom:12px}.breadcrumb a{color:#be9cff;cursor:pointer}.breadcrumb .sep{padding:0 6px;color:#68708c}.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px}.toolbar select{flex:1;min-width:150px;background:#101426;border:1px solid #353854;color:#d9dbea;border-radius:9px;padding:12px}.folder-list{height:min(58vh,540px);overflow:auto;border:1px solid #33364f;background:#0e1120;border-radius:12px}.folder-list::-webkit-scrollbar{width:9px}.folder-list::-webkit-scrollbar-thumb{background:#7141d6;border-radius:10px}.list-head{font-size:11px;color:#8f94ab;font-weight:800;letter-spacing:.1em;padding:14px 18px;border-bottom:1px solid var(--line)}.row{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid rgba(59,63,91,.55);cursor:pointer;transition:.15s}.row:hover,.row.selected{background:#201d40;box-shadow:inset 2px 0 #8758e9}.row:last-child{border-bottom:0}.row .check{accent-color:#8b5cf6;width:16px;height:16px}.row .ficon{font-size:20px}.row .fname{flex:1;font-size:14px}.row .fmeta{font-size:12px;color:var(--muted)}.row[data-folder=true] .fname:after{content:'›';float:right;font-size:22px;color:#a8a1bf;line-height:12px}.empty{text-align:center;padding:60px 20px;color:var(--muted)}.progress-panel{display:none;margin-top:15px;border:1px solid var(--line);border-radius:12px;padding:15px}.progress-panel.show{display:block}.pitem{display:flex;gap:8px;padding:5px}.pname{flex:1}.pstatus.ok{color:var(--green)}.pstatus.err{color:var(--red)}.help,.back{display:none}@media(max-width:900px){.app-sidebar{width:64px;padding:18px 8px}.brand span,.nav span,.section-label,.storage-card,.theme,.service-nav span{display:none}.brand{padding:0;justify-content:center}.nav a{text-align:center;justify-content:center}.main{margin-left:64px;width:calc(100% - 64px);padding:14px}.top{flex-wrap:wrap;height:auto}.search{width:100%}.user,.quick{display:none}.hero-head,.toolbar{align-items:stretch;flex-direction:column}.connection{justify-content:space-between}.folder-list{height:55vh}}
</style></head>
<body><div class="layout"><aside class="app-sidebar"><div class="brand"><b class="cloud-logo">☁</b><span>Cloud Storage</span></div><nav class="nav"><a href="/drive">▦ <span>Dashboard</span></a><a href="/drive" onclick="sessionStorage.setItem('cloudNav','recent')">◷ <span>Recent</span></a><a href="/drive" onclick="sessionStorage.setItem('cloudNav','favorites')">☆ <span>Favorites</span></a><a href="/drive" onclick="sessionStorage.setItem('cloudNav','shared')">♧ <span>Shared with me</span></a><a href="/drive" onclick="sessionStorage.setItem('cloudNav','trash')">♲ <span>Trash</span></a></nav><div class="section-label">STORAGE</div><a class="nav" href="/drive" style="text-decoration:none"><span class="nav a">☁ <span>Storage</span></span></a><div class="storage-card"><span class="infinity">∞</span><div><b>Unlimited Storage</b><small>Penyimpanan Tanpa Batas</small></div></div><div class="theme"><span>Dark Mode</span><span class="switch"><i></i></span></div><div class="service-nav"><a class="active" href="/gdrive">△ <span>Google Drive</span></a><a href="/photos">✿ <span>Google Photos</span></a><a href="#" onclick="doLogout()">↪ <span>Logout</span></a></div></aside><main class="main"><header class="top"><div class="search">⌕ <input id="searchInput" placeholder="Cari file atau folder..." oninput="filterItems()"><kbd>Ctrl /</kbd></div><span class="user" id="userEmail"></span><div class="quick"><a href="/profile">♙ Profile</a><a class="active" href="/gdrive">△ Google Drive</a><a href="/photos">✿ Google Photos</a></div><button onclick="doLogout()">↪ Logout</button></header><section class="drive-hero"><div class="hero-head"><div class="drive-title"><span class="drive-mark">△</span><div><h1>Google Drive</h1><p>Kelola, sinkronkan dan akses file Anda dengan mudah.</p></div></div><div class="connection"><span class="status-pill"><i class="status-dot" id="statusDot"></i><span id="statusText">Memeriksa koneksi...</span></span><button class="btn ghost" id="connectBtn" style="display:none" onclick="connectDrive()">Hubungkan</button><button class="btn ghost" id="disconnectBtn" style="display:none" onclick="disconnectDrive()">Putuskan</button></div></div><div id="browserSection" class="browser" style="display:none"><h2>My Drive</h2><div class="breadcrumb" id="breadcrumb"></div><div class="toolbar"><select id="folderSelect" onchange="goSelectedFolder()"></select><button class="btn" id="syncBtn" onclick="syncDrive()">⟳ Sinkron Otomatis</button><button class="btn ghost" id="copyBtn" onclick="copySelected()" disabled>➤ Copy ke Telegram (<span id="selCount">0</span>)</button><button class="btn ghost" id="pauseBtn" onclick="pauseSync()" style="display:none">⏸ Pause</button><button class="btn ghost" id="resumeBtn" onclick="resumeSync()" style="display:none">▶ Lanjut</button><button class="btn ghost" id="cancelBtn" onclick="cancelSync()" style="display:none">✕ Batal</button><button class="btn ghost" id="retryBtn" onclick="retrySync()" style="display:none">⟳ Retry Gagal</button></div><div id="syncStatus" style="display:none;margin-bottom:12px"></div><div class="folder-list"><div class="list-head">NAMA FOLDER</div><div id="fileList"></div></div><div class="progress-panel" id="progressPanel"><h3>Copy Progress</h3><div id="progressList"></div></div></div><div class="empty" id="notConnected" style="display:none"><p>Google Drive belum terhubung.</p><p style="margin-top:8px">Klik tombol Hubungkan untuk memulai.</p></div></section></main></div><script>
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());
let currentFolder='root';
let folderStack=[{id:'root',name:'My Drive'}];
let selected=new Set();
let allItems=[];

async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  checkStatus();
}
async function checkStatus(){
  const d=await api('/api/gdrive/status');
  const dot=document.getElementById('statusDot');
  const txt=document.getElementById('statusText');
  const connBtn=document.getElementById('connectBtn');
  const discBtn=document.getElementById('disconnectBtn');
  const browser=document.getElementById('browserSection');
  const nc=document.getElementById('notConnected');
  if(d.connected){
    dot.classList.add('on');
    txt.textContent='Terhubung ke Google Drive';
    connBtn.style.display='none';
    discBtn.style.display='inline-block';
    browser.style.display='block';
    nc.style.display='none';
    loadFiles('root');
  } else {
    dot.classList.remove('on');
    txt.textContent='Tidak terhubung';
    connBtn.style.display='inline-block';
    discBtn.style.display='none';
    browser.style.display='none';
    nc.style.display='block';
  }
}
function connectDrive(){location.href='/api/gdrive/auth';}
async function disconnectDrive(){
  // No server endpoint to delete tokens in spec; just clear locally via reload
  if(!confirm('Putuskan koneksi Google Drive?'))return;
  // attempt delete via a POST to a soft route; fallback to reload
  await api('/api/gdrive/disconnect',{method:'POST'}).catch(()=>{});
  checkStatus();
}
async function loadFiles(folderId){
  currentFolder=folderId;
  selected.clear();
  updateSelCount();
  const d=await api('/api/gdrive/files?folder_id='+encodeURIComponent(folderId));
  if(d.connected===false){checkStatus();return;}
  if(d.error){alert(d.error);return;}
  allItems=[...d.folders,...d.files];
  renderBreadcrumb();
  renderFolderSelect(d.folders);
  renderList(allItems);
}
function renderBreadcrumb(){
  let html='';
  folderStack.forEach((f,i)=>{
    if(i>0)html+='<span class="sep">/</span>';
    if(i===folderStack.length-1)html+='<span class="current">'+f.name+'</span>';
    else html+='<a onclick="navTo('+i+')">'+f.name+'</a>';
  });
  document.getElementById('breadcrumb').innerHTML=html;
}
function renderFolderSelect(folders){
  const sel=document.getElementById('folderSelect');
  sel.innerHTML='<option value="">📂 Masuk ke folder...</option>'+
    folders.map(f=>'<option value="'+f.id+'">📁 '+f.name+'</option>').join('');
}
function goSelectedFolder(){
  const v=document.getElementById('folderSelect').value;
  if(v){enterFolder(v);document.getElementById('folderSelect').value='';}
}
function navTo(idx){
  folderStack=folderStack.slice(0,idx+1);
  loadFiles(folderStack[idx].id);
}
function enterFolder(id){
  const f=allItems.find(x=>x.id===id&&x.is_folder);
  if(f){folderStack.push({id,name:f.name});loadFiles(id);}
}
function renderList(items){
  const el=document.getElementById('fileList');
  if(!items.length){
    el.innerHTML='<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg><p>Folder kosong</p></div>';
    return;
  }
  el.innerHTML=items.map(it=>{
    const icon=it.is_folder?'📁':it.is_google_doc?'📝':(it.mimeType.startsWith('image/')?'🖼️':it.mimeType.startsWith('video/')?'🎬':it.mimeType.startsWith('audio/')?'🎵':'📄');
    const meta=it.is_folder?'':(it.size_human||'');
    const cb=it.is_folder?'':'<input type="checkbox" class="check" data-id="'+it.id+'">';
    return'<div class="row '+(selected.has(it.id)?'selected':'')+'" data-folder="'+(it.is_folder?'true':'false')+'" data-id="'+it.id+'">'+
      cb+
      '<span class="ficon">'+icon+'</span>'+
      '<span class="fname">'+it.name+'</span>'+
      '<span class="fmeta">'+meta+'</span></div>';
  }).join('');
  el.querySelectorAll('.row').forEach(row=>{
    row.addEventListener('click',function(e){
      const id=this.dataset.id;
      const isFolder=this.dataset.folder==='true';
      if(e.target.classList.contains('check')){
        toggleSel(id,e.target);
      } else if(isFolder){
        enterFolder(id);
      }
    });
  });
}
function toggleSel(id,cb){
  if(cb.checked)selected.add(id);else selected.delete(id);
  cb.closest('.row').classList.toggle('selected',cb.checked);
  updateSelCount();
}
function updateSelCount(){
  document.getElementById('selCount').textContent=selected.size;
  document.getElementById('copyBtn').disabled=selected.size===0;
}
async function copySelected(){
  if(!selected.size)return;
  const panel=document.getElementById('progressPanel');
  const list=document.getElementById('progressList');
  panel.classList.add('show');list.innerHTML='';
  const ids=[...selected];
  list.innerHTML=ids.map(id=>{
    const it=allItems.find(x=>x.id===id);
    return'<div class="pitem" id="p_'+id+'"><span class="pname">'+ (it?it.name:id) +'</span><span class="pstatus">⏳ ...</span></div>';
  }).join('');
  const r=await fetch('/api/gdrive/copy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids:ids,folder_id:0}),credentials:'same-origin'});
  const d=await r.json();
  (d.results||[]).forEach(res=>{
    const id=res.id||res.name;
    const el=document.getElementById('p_'+id)||null;
    if(!el){
      // match by name fallback already rendered
      return;
    }
    const st=el.querySelector('.pstatus');
    if(res.ok){st.textContent='✅ '+res.size_human;st.className='pstatus ok';}
    else{st.textContent='❌ '+(res.error||'Gagal');st.className='pstatus err';}
  });
}
async function syncDrive(){
  const btn=document.getElementById('syncBtn');
  const st=document.getElementById('syncStatus');
  const pauseBtn=document.getElementById('pauseBtn');
  const cancelBtn=document.getElementById('cancelBtn');
  if(!confirm('Upload file dari Google Drive ke Telegram?'))return;
  btn.disabled=true;btn.textContent='Memulai...';
  st.style.display='block';
  try{
    const d=await api('/api/gdrive/sync?t='+Date.now());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">Error: '+d.error+'</span>';btn.disabled=false;btn.textContent='🔄 Sinkron Otomatis';return;}
    pauseBtn.style.display='inline-block';cancelBtn.style.display='inline-block';document.getElementById('retryBtn').style.display='none';
    pollSync();
  }catch(e){
    st.innerHTML='<span style="color:var(--danger)">Gagal: '+e+'</span>';
    btn.disabled=false;btn.textContent='🔄 Sinkron Otomatis';
  }
}
async function pauseSync(){
  await api('/api/gdrive/sync/pause');
  document.getElementById('pauseBtn').style.display='none';
  document.getElementById('resumeBtn').style.display='inline-block';
}
async function resumeSync(){
  await api('/api/gdrive/sync/resume');
  document.getElementById('resumeBtn').style.display='none';
  document.getElementById('pauseBtn').style.display='inline-block';
}
async function cancelSync(){
  if(!confirm('Batalkan sync?'))return;
  await api('/api/gdrive/sync/cancel');
  document.getElementById('pauseBtn').style.display='none';
  document.getElementById('resumeBtn').style.display='none';
  document.getElementById('cancelBtn').style.display='none';
}

let syncPollTimer=null;
function pollSync(){
  if(syncPollTimer)clearInterval(syncPollTimer);
  syncPollTimer=setInterval(async()=>{
    try{
      const s=await api('/api/gdrive/sync/status?t='+Date.now());
      const st=document.getElementById('syncStatus');
      const btn=document.getElementById('syncBtn');
      const pauseBtn=document.getElementById('pauseBtn');
      const resumeBtn=document.getElementById('resumeBtn');
      const cancelBtn=document.getElementById('cancelBtn');
      const pct=s.percent||0;
      let html='';
      if(s.running){
        html='<div style="margin-bottom:10px;font-size:14px"><strong>'+s.message+'</strong></div>';
        html+='<div style="height:28px;background:#0d0221;border-radius:14px;overflow:hidden;border:2px solid var(--border);margin-bottom:10px;position:relative">';
        html+='<div style="height:100%;width:'+pct+'%;background:linear-gradient(90deg,#7c3aed,#a855f7);transition:width .5s ease;border-radius:12px"></div>';
        html+='<div style="position:absolute;top:0;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:800;color:#fff;text-shadow:0 2px 6px rgba(0,0,0,.9)">'+pct+'%</div></div>';
        html+='<div style="font-size:13px;color:var(--muted)">'+s.done+' / '+s.total+' file &bull; '+s.imported+' terupload &bull; '+s.errors_count+' error</div>';
        if(s.current){html+='<div style="font-size:12px;color:var(--muted);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">📄 '+s.current+'</div>';}
        btn.disabled=true;btn.textContent='Sedang Sync...';
        pauseBtn.style.display=s.paused?'none':'inline-block';
        resumeBtn.style.display=s.paused?'inline-block':'none';
        cancelBtn.style.display='inline-block';
        if(s.paused){btn.textContent='Paused';}
      } else {
        clearInterval(syncPollTimer);syncPollTimer=null;
        btn.disabled=false;pauseBtn.style.display='none';resumeBtn.style.display='none';cancelBtn.style.display='none';document.getElementById('retryBtn').style.display=s.errors_count>0?'inline-block':'none';
        if(s.errors_count>0){
          btn.textContent='🔄 Sinkron Otomatis';
          html='<span style="color:var(--yellow)">Selesai dengan '+s.errors_count+' error</span>';
          html+='<div style="font-size:13px;color:var(--muted);margin-top:4px">'+s.imported+' file terupload &bull; '+s.errors_count+' gagal</div>';
          if(s.errors&&s.errors.length){
            html+='<div style="margin-top:8px;max-height:150px;overflow-y:auto;font-size:12px;color:var(--danger);background:rgba(239,68,68,.08);padding:8px;border-radius:8px">';
            s.errors.forEach(function(e){html+='&#8226; '+e+'<br>';});
            html+='</div>';
          }
        } else {
          btn.textContent='🔄 Sinkron Otomatis';
          html='<span style="color:var(--green)">'+s.message+'</span>';
          if(s.imported>0)html+='<div style="font-size:13px;color:var(--muted);margin-top:4px">'+s.imported+' file berhasil diupload</div>';
        }
      }
      st.innerHTML=html;
    }catch(e){}
  },1500);
}

// Auto-poll on page load if sync is running
setTimeout(async()=>{
  try{const s=await api('/api/gdrive/sync/status?t='+Date.now());if(s.running||s.total>0)pollSync();}catch(e){}
},1000);

async function doLogout(){await api('/api/logout',{method:'POST'});location.href='/';}
init();
</script></body></html>'''

PHOTOS_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage — Google Photos</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d0221;--surface:#1a1a2e;--accent:#a855f7;--green:#22c55e;--danger:#ef4444;--text:#e0e0e0;--muted:#888;--border:#6c3baa}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent)}
.topbar .right{display:flex;align-items:center;gap:12px}
.topbar .right button{padding:6px 14px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}
.wrap{max-width:1000px;margin:24px auto;padding:0 24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px}
.status-bar{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.status-dot{width:10px;height:10px;border-radius:50%;background:var(--danger)}
.status-dot.on{background:var(--green)}
.status-text{font-size:13px;color:var(--muted)}
.btn{padding:10px 18px;background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--accent)}
.btn.green{background:linear-gradient(135deg,#16a34a,#22c55e)}
.pick-section{display:flex;flex-direction:column;gap:14px;align-items:flex-start}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:12px;margin-top:16px}
.thumb{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:10px;overflow:hidden;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .fname{position:absolute;bottom:0;left:0;right:0;background:rgba(13,2,33,.85);font-size:10px;padding:4px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.thumb .rm{position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;background:rgba(13,2,33,.8);border:none;color:var(--danger);font-size:13px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.empty{text-align:center;padding:48px 20px;color:var(--muted)}
.empty svg{width:48px;height:48px;stroke:var(--border);margin-bottom:8px}
.selected-info{font-size:13px;color:var(--muted);margin-top:4px}
.action-bar{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap;align-items:center}
.bar{height:10px;background:var(--bg);border-radius:6px;overflow:hidden;margin-top:12px;border:1px solid var(--border)}
.bar > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#7c3aed,#a855f7);transition:width .25s}
.progress-panel{display:none;margin-top:16px;background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:16px;max-height:300px;overflow-y:auto}
.progress-panel.show{display:block}
.progress-panel h3{font-size:14px;color:var(--accent);margin-bottom:12px}
.pitem{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px}
.pitem .pname{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pitem .pstatus{font-size:11px;flex-shrink:0}
.pstatus.ok{color:var(--green)}.pstatus.err{color:var(--danger)}
.summary{font-size:13px;margin-top:12px;min-height:18px}
.summary.ok{color:var(--green)}.summary.err{color:var(--danger)}
.help{margin-top:16px;font-size:12px;color:var(--muted);line-height:1.6}
.help code{background:var(--bg);padding:2px 6px;border-radius:4px;color:var(--accent)}
.back{display:inline-block;margin-top:16px;color:var(--accent);text-decoration:none;font-size:13px}
.tab-btn{opacity:.6;transition:opacity .2s}.tab-btn.active-tab{opacity:1;border-color:var(--accent)!important;background:rgba(168,85,247,.15)!important}
</style>
</head>
<body>
<div class="topbar">
  <h1>📸 Google Photos</h1>
  <div class="right">
    <button onclick="doLogout()">Logout</button>
  </div>
</div>
<div class="wrap">
  <div class="card">
    <div class="status-bar">
      <span class="status-dot" id="statusDot"></span>
      <span class="status-text" id="statusText">Memeriksa koneksi...</span>
      <button class="btn ghost" id="connectBtn" style="display:none" onclick="connectPhotos()">🔗 Hubungkan Google Photos</button>
      <button class="btn ghost" id="disconnectPhotosBtn" style="display:none" onclick="disconnectPhotos()">⏏ Putuskan</button>
    </div>

    <div id="connectedSection" style="display:none">
      <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
        <button class="btn ghost tab-btn active-tab" id="tabPicker" type="button" onclick="showTab('picker')">🖼️ Google Photos Picker</button>
        <button class="btn ghost tab-btn" id="tabDrive" type="button" onclick="showTab('drive')">📁 Foto dari Drive</button>
      </div>

      <div id="pickerPanel">
        <div class="pick-section">
          <button class="btn" id="pickBtn" onclick="openPicker()">🖼️ Pilih Foto dari Google Photos</button>
          <span class="selected-info" id="selInfo">Belum ada foto dipilih.</span>
          <div class="grid" id="grid"></div>
        </div>
      </div>

      <div id="drivePanel" style="display:none">
        <p style="color:var(--muted);font-size:13px;margin-bottom:12px">Foto & video dari Google Drive (termasuk backup Google Photos yang sync ke Drive).</p>
        <button class="btn" id="driveListBtn" onclick="loadDrivePhotos()">📁 Muat Foto dari Drive</button>
        <span class="selected-info" id="driveStatus"></span>
        <div class="grid" id="driveGrid"></div>
        <div class="action-bar" style="margin-top:12px">
          <button class="btn green" id="driveImportBtn" onclick="importDriveSelected()" disabled>📤 Import ke Telegram (<span id="driveSelCount">0</span>)</button>
          <span class="summary" id="driveSummary"></span>
        </div>
      </div>

      <div class="action-bar" id="pickerActionBar">
        <button class="btn green" id="importBtn" onclick="importSelected()" disabled>📤 Import ke Telegram (<span id="selCount">0</span>)</button>
        <span class="summary" id="summary"></span>
      </div>
      <div class="bar" id="pickerBar"><i id="barFill"></i></div>

      <div class="progress-panel" id="progressPanel">
        <h3>📤 Import Progress</h3>
        <div id="progressList"></div>
      </div>
    </div>

    <div class="empty" id="notConnected" style="display:none">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      <p>Belum terhubung ke Google Photos.</p>
      <p style="margin-top:6px">Klik "Hubungkan Google Photos" untuk memulai.</p>
    </div>

    <div class="help">
      <strong>Google Photos Picker:</strong><br>
      Pilih foto/video di jendela resmi Google, lalu kembali ke halaman ini untuk mengimpornya ke Telegram.<br>
      Jika Google meminta izin baru, setujui akses <code>Google Photos Picker API</code>.
    </div>
    <a class="back" href="/files">← Kembali ke Drive</a>
  </div>
</div>
<script>
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());
let items=[];               // {id, name, baseUrl}
let pickerWin=null;
let pollTimer=null;

async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  checkStatus();
}
async function checkStatus(){
  const d=await api('/api/photos/status');
  const dot=document.getElementById('statusDot');
  const txt=document.getElementById('statusText');
  const connBtn=document.getElementById('connectBtn');
  const cs=document.getElementById('connectedSection');
  const nc=document.getElementById('notConnected');
  if(d.connected){
    dot.classList.add('on');
    txt.textContent='Terhubung ke Google Photos';
    connBtn.style.display='none';
    var dBtn=document.getElementById('disconnectPhotosBtn');if(dBtn)dBtn.style.display='inline-block';
    cs.style.display='block';
    nc.style.display='none';
  } else {
    dot.classList.remove('on');
    txt.textContent='Tidak terhubung';
    connBtn.style.display='inline-block';
    var dBtn2=document.getElementById('disconnectPhotosBtn');if(dBtn2)dBtn2.style.display='none';
    cs.style.display='none';
    nc.style.display='block';
  }
}
function connectPhotos(){location.href='/api/photos/auth';}
async function disconnectPhotos(){if(!confirm('Putuskan Google Photos?'))return;await fetch('/api/photos/disconnect',{method:'POST'});location.reload();}
async function openPicker(){
  const btn=document.getElementById('pickBtn');
  btn.disabled=true;btn.textContent='⏳ Membuat sesi picker...';
  try{
    const d=await api('/api/photos/picker/create',{method:'POST'});
    if(d.error){alert(d.error);return;}
    if(!d.picker_uri){
      // Fallback: some backend redirects; let server handle window
      if(d.picker_url){location.href=d.picker_url;return;}
      alert('picker_uri tidak tersedia');return;
    }
    items=[];renderGrid();updateSelCount();
    document.getElementById('summary').textContent='';
    pollTimer=null;
    pickerWin=window.open(d.picker_uri,'gphotos_picker','width=720,height=620');
    if(!pickerWin){alert('Popup diblokir. Izinkan popup untuk membuka Google Photos Picker.');return;}
    startPolling();
  } catch(e){
    alert('Gagal membuka picker: '+e);
  } finally {
    btn.disabled=false;btn.textContent='🖼️ Pilih Foto dari Google Photos';
  }
}
function startPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{
    try{
      const d=await api('/api/photos/picker/items');
      if(d.done || (d.items && d.items.length)){
        if(d.items && d.items.length){ items=d.items; renderGrid(); updateSelCount(); }
      }
      if(d.done){
        stopPolling();
        if(pickerWin && !pickerWin.closed)pickerWin.close();
        if(!items.length)document.getElementById('selInfo').textContent='Tidak ada foto dipilih.';
      }
    }catch(e){/* keep polling */}
  },2000);
}
function stopPolling(){
  if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
}
function thumbUrl(b){return b?(b+'=w160-h160-c'):'';}
function renderGrid(){
  const el=document.getElementById('grid');
  if(!items.length){el.innerHTML='';return;}
  el.innerHTML=items.map((it,i)=>{
    const u=thumbUrl(it.baseUrl);
    return '<div class="thumb">'+
      '<img src="'+u+'" alt="'+ (it.name||'photo') +'" loading="lazy" onerror="this.style.opacity=0.2">'+
      '<button class="rm" onclick="removeItem('+i+')" title="Hapus">✕</button>'+
      (it.name?'<span class="fname">'+it.name+'</span>':'')+
    '</div>';
  }).join('');
}
function removeItem(i){
  items.splice(i,1);renderGrid();updateSelCount();
  document.getElementById('summary').textContent='';
}
function updateSelCount(){
  document.getElementById('selCount').textContent=items.length;
  document.getElementById('importBtn').disabled=items.length===0;
  document.getElementById('selInfo').textContent=items.length
    ? items.length+' foto dipilih.'
    : 'Belum ada foto dipilih.';
}
async function importSelected(){
  if(!items.length)return;
  const panel=document.getElementById('progressPanel');
  const list=document.getElementById('progressList');
  const bar=document.getElementById('barFill');
  const summary=document.getElementById('summary');
  const btn=document.getElementById('importBtn');
  panel.classList.add('show');list.innerHTML='';summary.textContent='';summary.className='summary';
  bar.style.width='0%';
  const ids=items.map(it=>it.id);
  list.innerHTML=items.map(it=>
    '<div class="pitem" id="p_'+it.id+'"><span class="pname">'+ (it.name||it.id) +'</span><span class="pstatus">⏳ ...</span></div>'
  ).join('');
  btn.disabled=true;
  try{
    const r=await fetch('/api/photos/picker/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids}),credentials:'same-origin'});
    const d=await r.json();
    const results=d.results||[];
    let ok=0,err=0;
    results.forEach((res,i)=>{
      const el=document.getElementById('p_'+res.id);
      const st=el?el.querySelector('.pstatus'):null;
      if(res.ok){ok++;if(st){st.textContent='✅';st.className='pstatus ok';}}
      else{err++;if(st){st.textContent='❌ '+(res.error||'Gagal');st.className='pstatus err';}}
      bar.style.width=Math.round(((i+1)/results.length)*100)+'%';
    });
    if(err===0)summary.textContent='✅ Semua berhasil diimport ('+ok+')';
    else summary.textContent=(ok?('✅ '+ok+' berhasil. '):'')+'❌ '+err+' gagal.';
    summary.className='summary '+(err===0?'ok':'err');
  }catch(e){
    summary.textContent='❌ '+e;summary.className='summary err';
  }finally{
    btn.disabled=items.length===0;
  }
}
async function doLogout(){await api('/api/logout',{method:'POST'});location.href='/';}

/* Tab system */
let activeTab='picker';
function showTab(t){
  activeTab=t;
  document.getElementById('pickerPanel').style.display=t==='picker'?'':'none';
  document.getElementById('pickerActionBar').style.display=t==='picker'?'':'none';
  document.getElementById('pickerBar').style.display=t==='picker'?'':'none';
  document.getElementById('drivePanel').style.display=t==='drive'?'':'none';
  document.getElementById('tabPicker').classList.toggle('active-tab',t==='picker');
  document.getElementById('tabDrive').classList.toggle('active-tab',t==='drive');
}

/* Google Drive photos */
let drivePhotos=[];
let driveSelected=new Set();

async function loadDrivePhotos(){
  const btn=document.getElementById('driveListBtn');
  const status=document.getElementById('driveStatus');
  const grid=document.getElementById('driveGrid');
  btn.disabled=true;btn.textContent='Memuat...';
  status.textContent='Memuat foto dari Drive...';
  grid.innerHTML='';
  drivePhotos=[];driveSelected.clear();
  try{
    const d=await api('/api/photos/drive-list');
    if(d.error){status.textContent='Error: '+d.error;btn.disabled=false;btn.textContent='Muat Foto dari Drive';return;}
    drivePhotos=d.files||[];
    status.textContent=drivePhotos.length+' foto ditemukan. Klik foto untuk memilih.';
    renderDriveGrid();
  }catch(e){
    status.textContent='Gagal: '+e;
  }
  btn.disabled=false;btn.textContent='Muat Foto dari Drive';
}

function renderDriveGrid(){
  const grid=document.getElementById('driveGrid');
  grid.innerHTML='';
  drivePhotos.forEach((f,i)=>{
    const div=document.createElement('div');
    div.className='thumb'+(driveSelected.has(i)?' selected':'');
    div.style.border=driveSelected.has(i)?'2px solid var(--green)':'';
    div.onclick=()=>{if(driveSelected.has(i))driveSelected.delete(i);else driveSelected.add(i);renderDriveGrid();updateDriveSelCount();};
    let inner='';
    if(f.thumbnail){
      inner='<img src="'+f.thumbnail+'" alt="'+f.name+'" loading="lazy">';
    } else if(f.mimeType&&f.mimeType.startsWith('video/')){
      inner='<div style="font-size:28px">&#127909;</div>';
    } else {
      inner='<div style="font-size:28px">&#128196;</div>';
    }
    const sz=f.size>0?(f.size>1048576?(f.size/1048576).toFixed(1)+'MB':(f.size/1024).toFixed(0)+'KB'):'';
    div.innerHTML=inner+'<div class="fname">'+f.name+(sz?' ('+sz+')':'')+'</div>';
    if(driveSelected.has(i)){
      div.innerHTML+='<div class="rm" style="background:var(--green);color:#fff">&#10003;</div>';
    }
    grid.appendChild(div);
  });
}

function updateDriveSelCount(){
  document.getElementById('driveSelCount').textContent=driveSelected.size;
  document.getElementById('driveImportBtn').disabled=driveSelected.size===0;
  document.getElementById('driveImportBtn').textContent='Import ke Telegram ('+driveSelected.size+')';
}

async function importDriveSelected(){
  if(!driveSelected.size)return;
  const btn=document.getElementById('driveImportBtn');
  const summary=document.getElementById('driveSummary');
  const ids=[...driveSelected].map(i=>drivePhotos[i].id);
  btn.disabled=true;btn.textContent='Importing...';
  summary.textContent='Mengimport '+ids.length+' foto ke Telegram...';
  try{
    const d=await api('/api/photos/drive-import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids:ids})});
    if(d.error){summary.className='summary err';summary.textContent='Error: '+d.error;}
    else{
      summary.className='summary ok';
      summary.textContent='Berhasil import '+d.imported+' foto!'+(d.errors&&d.errors.length?' ('+d.errors.length+' error)':'');
      driveSelected.clear();renderDriveGrid();updateDriveSelCount();
    }
  }catch(e){
    summary.className='summary err';summary.textContent='Gagal: '+e;
  }
  btn.disabled=false;
}

init();
</script>
</body>
</html>'''

@drive_ext.route("/api/debug/cfg")
def debug_cfg():
    return jsonify({"gdrive_id": bool(cfg.get("GDRIVE_CLIENT_ID")), "gdrive_secret": bool(cfg.get("GDRIVE_CLIENT_SECRET")), "cfg_len": len(cfg), "keys": list(cfg.keys())[:10]})

# ============================================================
# GOOGLE PHOTOS PICKER API
# ============================================================
import urllib.request

PHOTOS_SCOPES = SCOPES
_picker_sessions = {}

def _photos_client_config():
    cid = cfg.get("GDRIVE_CLIENT_ID")
    csec = cfg.get("GDRIVE_CLIENT_SECRET")
    if not cid or not csec:
        return None
    return {"web": {"client_id": cid, "client_secret": csec, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https"))]}}

def _get_photos_credentials(uid):
    _ensure_cfg_loaded()
    if not _HAS_GOOGLE:
        return None
    row = db_query("SELECT access_token, refresh_token, token_expiry FROM google_tokens WHERE user_id=?", (uid,))
    if not row:
        return None
    r = row[0]
    creds = Credentials(token=r["access_token"], refresh_token=r["refresh_token"], token_uri="https://oauth2.googleapis.com/token", client_id=cfg.get("GDRIVE_CLIENT_ID"), client_secret=cfg.get("GDRIVE_CLIENT_SECRET"), scopes=PHOTOS_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            db_exec("UPDATE google_tokens SET access_token=?, token_expiry=? WHERE user_id=?", (creds.token, creds.expiry.isoformat(), uid))
        except Exception:
            return None
    return creds

def _get_photos_headers(uid):
    creds = _get_photos_credentials(uid)
    if not creds or not creds.valid:
        return None
    return {"Authorization": "Bearer " + creds.token}

@drive_ext.route("/api/photos/status")
def photos_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    creds = _get_photos_credentials(uid)
    return jsonify({"connected": bool(creds and creds.valid)})

@drive_ext.route("/api/photos/disconnect", methods=["POST"])
def photos_disconnect():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    db_exec("DELETE FROM google_tokens WHERE user_id=?", (uid,))
    return jsonify({"ok": True})

@drive_ext.route("/api/photos/drive-list")
def photos_drive_list():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    
    creds = _get_credentials(uid)
    if not creds:
        return jsonify({"error": "not connected"}), 401
    
    try:
        from googleapiclient.discovery import build
        service = build('drive', 'v3', credentials=creds)
        
        # List image/video files from Google Photos backup in Drive
        # Google Photos syncs to Drive in "Google Photos" folder
        results = service.files().list(
            q="(mimeType contains 'image/' or mimeType contains 'video/') and trashed = false",
            fields="files(id,name,mimeType,size,modifiedTime,thumbnailLink,parents)",
            pageSize=50,
            orderBy="modifiedTime desc"
        ).execute()
        
        files = results.get('files', [])
        items = []
        for f in files:
            size = int(f.get('size', 0))
            thumb = f.get('thumbnailLink', '')
            # Replace thumbnail size params with larger
            if thumb:
                thumb = thumb.replace('=s220', '=s400')
            items.append({
                'id': f['id'],
                'name': f['name'],
                'mimeType': f['mimeType'],
                'size': size,
                'modifiedTime': f.get('modifiedTime', ''),
                'thumbnail': thumb,
            })
        
        return jsonify({"files": items, "count": len(items)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@drive_ext.route("/api/photos/drive-import", methods=["POST"])
def photos_drive_import():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    
    data = request.get_json()
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "no files"}), 400
    
    creds = _get_gdrive_credentials(uid)
    if not creds:
        return jsonify({"error": "not connected"}), 401
    
    try:
        from googleapiclient.discovery import build
        import io
        from googleapiclient.http import MediaIoBaseDownload
        
        service = build('drive', 'v3', credentials=creds)
        
        # Get channel
        ch_row = db_query("SELECT id FROM channels WHERE user_id=? LIMIT 1", (uid,))
        if not ch_row:
            return jsonify({"error": "channel not found"}), 404
        
        channel_id = ch_row[0]["id"]
        
        imported = 0
        errors = []
        
        for fid in file_ids:
            try:
                # Get file metadata
                meta = service.files().get(fileId=fid, fields="name,mimeType,size").execute()
                name = meta['name']
                mime = meta['mimeType']
                size = int(meta.get('size', 0))
                
                # Download
                request_dl = service.files().get_media(fileId=fid)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request_dl)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
                
                fh.seek(0)
                data_bytes = fh.read()
                
                # Upload to Telegram channel
                from telethon import TelegramClient
                client = _get_telethon_client()
                if not client:
                    errors.append(f"{name}: client error")
                    continue
                
                # Save to temp
                import tempfile
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(name)[1])
                tmp.write(data_bytes)
                tmp.close()
                
                # Upload to Telegram
                async def upload_to_tg():
                    await client.send_file(
                        int(channel_id),
                        tmp.name,
                        caption=name,
                        force_document=False
                    )
                
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(upload_to_tg())
                loop.close()
                
                os.unlink(tmp.name)
                imported += 1
                
            except Exception as e:
                errors.append(f"{name}: {str(e)}")
        
        return jsonify({"imported": imported, "errors": errors})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@drive_ext.route("/api/photos/auth")
def photos_auth():
    # Force-load config.env if GDRIVE keys missing
    _env = os.path.join(BASE, "config.env")
    if os.path.exists(_env) and not cfg.get("GDRIVE_CLIENT_ID"):
        with open(_env) as _f:
            for _l in _f:
                _l = _l.strip()
                if not _l or _l.startswith("#") or "=" not in _l: continue
                _k, _v = _l.split("=", 1)
                cfg[_k.strip()] = _v.strip()
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    if not _HAS_GOOGLE:
        return jsonify({"error": "google libs not installed"}), 500
    client_config = _client_config()
    if not client_config:
        return jsonify({"error": "Google credentials not configured"}), 500
    flow = Flow.from_client_config(client_config, scopes=PHOTOS_SCOPES, redirect_uri=cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https")))
    state = "photos:" + secrets.token_urlsafe(16)
    session["photos_state"] = state
    session["photos_uid"] = uid
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    if hasattr(flow, "code_verifier") and flow.code_verifier:
        session["photos_code_verifier"] = flow.code_verifier
    session["photos_scopes"] = PHOTOS_SCOPES
    return redirect(auth_url)

def _photos_callback_handler(uid):
    expected_state = session.pop("photos_state", None)
    state = request.args.get("state", "")
    if expected_state and state != expected_state:
        return jsonify({"error": "state mismatch"}), 400
    if not expected_state:
        return jsonify({"error": "auth session expired"}), 400
    client_config = _client_config()
    flow = Flow.from_client_config(client_config, scopes=PHOTOS_SCOPES, redirect_uri=cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https")), state=expected_state)
    saved_verifier = session.pop("photos_code_verifier", None)
    if saved_verifier:
        flow.code_verifier = saved_verifier
    try:
        _auth_resp = request.url.replace("http://", "https://", 1)
        flow.fetch_token(authorization_response=_auth_resp)
    except Exception as e:
        return jsonify({"error": "token exchange failed: " + str(e)}), 400
    creds = flow.credentials
    expiry = creds.expiry.isoformat() if creds.expiry else None
    db_exec("INSERT INTO google_tokens (user_id, access_token, refresh_token, token_expiry) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token, refresh_token=COALESCE(excluded.refresh_token, google_tokens.refresh_token), token_expiry=excluded.token_expiry", (uid, creds.token, creds.refresh_token, expiry))
    session.pop("photos_state", None)
    session.pop("photos_uid", None)
    return redirect("/drive#gphotos")

@drive_ext.route("/api/photos/picker/create", methods=["POST"])
def photos_picker_create():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    headers = _get_photos_headers(uid)
    if not headers:
        return jsonify({"error": "not connected"}), 401
    req = urllib.request.Request("https://photospicker.googleapis.com/v1/sessions", data=json.dumps({}).encode(), headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        sid = data.get("id")
        uri = data.get("pickerUri")
        if sid and uri:
            _picker_sessions[uid] = {"session_id": sid, "status": "pending"}
            return jsonify({"ok": True, "session_id": sid, "picker_uri": uri})
        return jsonify({"error": "invalid response"}), 500
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.readable() else ""
        try:
            err = json.loads(err_body)
        except Exception:
            err = {}
        return jsonify({"error": err.get("error", {}).get("message", str(e))}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@drive_ext.route("/api/photos/picker/items")
def photos_picker_items():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    headers = _get_photos_headers(uid)
    if not headers:
        return jsonify({"error": "not connected"}), 401
    ps = _picker_sessions.get(uid)
    if not ps:
        return jsonify({"error": "no active session"}), 400
    items = []
    page_token = None
    while True:
        url = "https://photospicker.googleapis.com/v1/mediaItems?sessionId=" + ps["session_id"] + "&pageSize=100"
        if page_token:
            url += "&pageToken=" + page_token
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            items.extend(data.get("mediaItems", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        except Exception:
            break
    return jsonify({"items": items, "count": len(items), "session_id": ps["session_id"]})

@drive_ext.route("/api/photos/picker/import", methods=["POST"])
def photos_picker_import():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    headers = _get_photos_headers(uid)
    if not headers:
        return jsonify({"error": "not connected"}), 401
    ps = _picker_sessions.get(uid)
    if not ps:
        return jsonify({"error": "no active session"}), 400
    data = request.get_json(silent=True) or {}
    item_ids = data.get("item_ids", [])
    items = []
    url = "https://photospicker.googleapis.com/v1/mediaItems?sessionId=" + ps["session_id"] + "&pageSize=100"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        items = data.get("mediaItems", [])
    except Exception:
        return jsonify({"error": "failed to get items"}), 500
    if item_ids:
        items = [i for i in items if i.get("id") in item_ids]
    # Keep Picker imports inside the account's existing backup root, never Home.
    backup_name = "Backup - " + uid
    db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, 0)", (backup_name,))
    root_rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=0", (backup_name,))
    backup_root_id = root_rows[0]["id"] if root_rows else 0
    db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)", ("Google Photos", backup_root_id))
    rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=?", ("Google Photos", backup_root_id))
    folder_id = rows[0]["id"] if rows else backup_root_id
    imported = 0
    errors = []
    for item in items:
        try:
            base_url = item.get("baseUrl", "")
            if not base_url:
                continue
            dl_url = base_url + "=d"
            dl_req = urllib.request.Request(dl_url, headers=headers)
            dl_resp = urllib.request.urlopen(dl_req, timeout=120)
            file_data = dl_resp.read()
            filename = item.get("filename", "photo.jpg")
            mime = item.get("mimeType", "image/jpeg")
            from web import telethon_client as _tc, run_async as _ra, CHANNEL as _ch
            forwarded = _ra(_tc.send_file(_ch, file=file_data, caption="photos:" + str(folder_id) + ":" + filename, force_document=True))
            db_exec("INSERT INTO files (msg_id, file_name, size, mime, folder_id) VALUES (?, ?, ?, ?, ?)", (forwarded.id, filename, len(file_data), mime, folder_id))
            imported += 1
        except Exception as e:
            errors.append(item.get("filename", "?") + ": " + str(e)[:100])
    try:
        del_req = urllib.request.Request("https://photospicker.googleapis.com/v1/sessions/" + ps["session_id"], headers=headers, method="DELETE")
        urllib.request.urlopen(del_req, timeout=10)
    except Exception:
        pass
    _picker_sessions.pop(uid, None)
    return jsonify({"ok": True, "imported": imported, "errors": errors, "total": len(items)})

# ------------------------------------------------------------------------------
# ============================================================================
#  SETTINGS FEATURES
# ============================================================================
# Allowed user ids that may view/modify settings
SETTINGS_ALLOWED_IDS = {"5337119189", "1032673884", "bowor4751@gmail.com"}

# Config keys managed by the Settings page
SETTINGS_KEYS = [
    "API_ID", "API_HASH", "BOT_TOKEN", "CHANNEL",
    "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET",
    "PHOTOS_CLIENT_ID", "PHOTOS_CLIENT_SECRET",
]

# Keys whose values are secrets and must be masked in GET responses
SETTINGS_SECRET_KEYS = {"API_HASH", "BOT_TOKEN", "GDRIVE_CLIENT_SECRET", "PHOTOS_CLIENT_SECRET"}


def _config_env_path():
    return os.path.join(BASE, "config.env")


def _mask_secret(value):
    """Mask a secret, revealing only the last 4 characters."""
    if value is None:
        return ""
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value) if value else ""
    return "*" * (len(value) - 4) + value[-4:]


def _read_config_env():
    """Read config.env preserving comments and blank lines.

    Returns (parsed_dict, raw_lines). parsed_dict maps KEY -> value for
    key=value lines; raw_lines is the full list of original text lines.
    """
    path = _config_env_path()
    parsed = {}
    raw_lines = []
    if os.path.exists(path):
        with open(path, "r") as f:
            raw_lines = f.readlines()
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            parsed[k.strip()] = v.strip()
    return parsed, raw_lines


def _write_config_env(values):
    """Write config values back to config.env, preserving comments/blank lines.

    Only keys already present in the file (or in SETTINGS_KEYS) are updated;
    new keys from values are appended. Comments and ordering of existing keys
    are preserved.
    """
    parsed, raw_lines = _read_config_env()
    # Update the in-memory parsed map
    for k, v in values.items():
        parsed[k] = v

    updated_keys = set()
    new_lines = []
    for line in raw_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in values:
                # Preserve trailing newline style of the original line
                nl = line[len(line.rstrip("\r\n")):] or "\n"
                new_lines.append(f"{k}={values[k]}{nl}")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    # Append any new keys not already present in the file
    for k in SETTINGS_KEYS:
        if k in values and k not in updated_keys:
            new_lines.append(f"{k}={values[k]}\n")

    path = _config_env_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(new_lines)
    os.replace(tmp, path)


@drive_ext.route("/settings")
def settings_page():
    """Render the Settings configuration page."""
    uid = session.get("user_id")
    if uid not in SETTINGS_ALLOWED_IDS:
        return redirect(url_for("login_page"))
    return SETTINGS_HTML


@drive_ext.route("/api/settings", methods=["GET"])
def api_settings_get():
    uid = session.get("user_id")
    if uid not in SETTINGS_ALLOWED_IDS:
        return jsonify({"error": "forbidden"}), 403
    _ensure_cfg_loaded()
    parsed, _ = _read_config_env()
    # Merge with in-memory cfg so freshly-saved values are reflected
    merged = dict(parsed)
    for k in SETTINGS_KEYS:
        if cfg.get(k):
            merged[k] = cfg[k]
    result = {}
    for k in SETTINGS_KEYS:
        val = merged.get(k, "")
        if k in SETTINGS_SECRET_KEYS:
            result[k] = _mask_secret(val)
        else:
            result[k] = val
    return jsonify({"settings": result})


@drive_ext.route("/api/settings", methods=["POST"])
def api_settings_post():
    uid = session.get("user_id")
    if uid not in SETTINGS_ALLOWED_IDS:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "invalid body"}), 400

    values = {}
    for k in SETTINGS_KEYS:
        if k in data:
            v = data[k]
            values[k] = v if v is None else str(v)

    # Validate: API_ID must be int
    if "API_ID" in values:
        try:
            int(str(values["API_ID"]).strip())
        except (ValueError, TypeError):
            return jsonify({"error": "API_ID must be an integer"}), 400

    # Validate: CHANNEL must be int (allow empty / __AUTO_CREATE__ passthrough)
    if "CHANNEL" in values:
        ch = str(values["CHANNEL"]).strip()
        if ch and ch != "__AUTO_CREATE__":
            try:
                int(ch)
            except (ValueError, TypeError):
                return jsonify({"error": "CHANNEL must be an integer"}), 400

    try:
        _write_config_env(values)
    except OSError as e:
        return jsonify({"error": f"failed to write config: {e}"}), 500

    # Refresh in-memory cfg so the running process picks up the new values
    _ensure_cfg_loaded()
    for k, v in values.items():
        cfg[k] = v

    return jsonify({"ok": True, "settings": {
        k: (_mask_secret(values[k]) if k in SETTINGS_SECRET_KEYS else values[k])
        for k in values
    }})


@drive_ext.route("/api/settings/test-telegram", methods=["POST"])
def api_settings_test_telegram():
    uid = session.get("user_id")
    if uid not in SETTINGS_ALLOWED_IDS:
        return jsonify({"error": "forbidden"}), 403
    _ensure_cfg_loaded()

    api_id = cfg.get("API_ID")
    api_hash = cfg.get("API_HASH")
    bot_token = cfg.get("BOT_TOKEN")

    if not (api_id and api_hash and bot_token):
        return jsonify({
            "ok": False,
            "connected": False,
            "username": None,
            "error": "Missing Telegram credentials (API_ID, API_HASH, BOT_TOKEN)",
        })

    # Try to reach Telegram Bot API using the configured token
    try:
        import urllib.request
        import urllib.error
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("ok"):
            return jsonify({
                "ok": True,
                "connected": True,
                "username": payload.get("result", {}).get("username"),
            })
        return jsonify({
            "ok": False,
            "connected": False,
            "username": None,
            "error": payload.get("description", "Telegram API returned ok=false"),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "connected": False,
            "username": None,
            "error": str(e),
        })


@drive_ext.route("/api/settings/restart", methods=["POST"])
def api_settings_restart():
    uid = session.get("user_id")
    if uid not in SETTINGS_ALLOWED_IDS:
        return jsonify({"error": "forbidden"}), 403
    try:
        # Kill any running web processes, then let systemd restart the service
        os.system("pkill -f 'web.py' 2>/dev/null; pkill -f 'web_drive' 2>/dev/null")
        rc = os.system("systemctl restart telegram-cloud-web")
        if rc != 0:
            return jsonify({
                "ok": False,
                "error": "systemctl restart failed (rc=%d)" % rc,
            }), 500
        return jsonify({"ok": True, "message": "Service restart requested."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


SETTINGS_HTML = '''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud Storage — Pengaturan</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d0221;--card:#1a0a3e;--border:#6c3baa;--accent:#a78bfa;--accent2:#8b5cf6;--text:#e0e0e0;--muted:#6b7280;--danger:#f87171;--green:#34d399}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:#1a0a3e;border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent)}
.topbar .right{display:flex;align-items:center;gap:12px}
.topbar .right button{padding:6px 14px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}
.wrap{max-width:720px;margin:32px auto;padding:0 24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px}
.card h2{font-size:16px;color:var(--accent);margin-bottom:20px}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:13px;color:var(--accent);margin-bottom:6px;font-weight:500}
.form-group input{width:100%;padding:12px 14px;background:#0d0221;border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;transition:.2s;font-family:inherit}
.form-group input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(167,139,250,.2)}
.btn-save{width:100%;padding:14px;background:linear-gradient(135deg,#6c3baa,#a78bfa);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:.2s;margin-top:8px}
.btn-save:hover{opacity:.9}
.btn-save:disabled{opacity:.5;cursor:not-allowed}
.btn-row{display:flex;gap:12px;margin-top:16px}
.btn-row button{flex:1;padding:12px;background:transparent;border:1px solid var(--border);border-radius:10px;color:var(--accent);font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.btn-row button:hover{background:rgba(167,139,250,.1)}
.btn-row button:disabled{opacity:.5;cursor:not-allowed}
.msg{font-size:13px;margin-top:14px;text-align:center;min-height:18px}
.msg.ok{color:var(--green)}
.msg.err{color:var(--danger)}
.back{display:inline-block;margin-top:16px;color:var(--accent);text-decoration:none;font-size:13px}
</style>
</head>
<body>
<div class="topbar">
  <h1>☁️ Cloud Storage</h1>
  <div class="right">
    <button onclick="doLogout()">Logout</button>
  </div>
</div>
<div class="wrap">
  <div class="card">
    <h2>⚙️ Pengaturan</h2>
    <form id="settingsForm" onsubmit="saveSettings(event)">
      <div class="form-group">
        <label>API ID</label>
        <input type="text" id="API_ID" inputmode="numeric" placeholder="1234567">
      </div>
      <div class="form-group">
        <label>API HASH</label>
        <input type="text" id="API_HASH" placeholder="abc123...">
      </div>
      <div class="form-group">
        <label>BOT TOKEN</label>
        <input type="text" id="BOT_TOKEN" placeholder="123456:ABC-DEF...">
      </div>
      <div class="form-group">
        <label>CHANNEL</label>
        <input type="text" id="CHANNEL" inputmode="numeric" placeholder="-1001234567 (atau __AUTO_CREATE__)">
      </div>
      <div class="form-group">
        <label>Google Drive Client ID</label>
        <input type="text" id="GDRIVE_CLIENT_ID" placeholder="....apps.googleusercontent.com">
      </div>
      <div class="form-group">
        <label>Google Drive Client Secret</label>
        <input type="text" id="GDRIVE_CLIENT_SECRET" placeholder="GOCSPX-...">
      </div>
      <div class="form-group">
        <label>Google Photos Client ID</label>
        <input type="text" id="PHOTOS_CLIENT_ID" placeholder="....apps.googleusercontent.com">
      </div>
      <div class="form-group">
        <label>Google Photos Client Secret</label>
        <input type="text" id="PHOTOS_CLIENT_SECRET" placeholder="GOCSPX-...">
      </div>
      <button type="submit" class="btn-save" id="saveBtn">💾 Simpan Pengaturan</button>
    </form>
    <div class="btn-row">
      <button id="testBtn" onclick="testTelegram()">🔌 Tes Koneksi Telegram</button>
      <button id="restartBtn" onclick="restartService()">🔄 Restart Layanan</button>
    </div>
    <div class="msg" id="msg"></div>
  </div>
  <a class="back" href="/drive">← Kembali ke Drive</a>
</div>
<script>
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());
async function doLogout(){await api('/api/logout',{method:'POST'});location.href='/';}
async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  loadSettings();
}
async function loadSettings(){
  const d=await api('/api/settings');
  if(!d.settings)return;
  const s=d.settings;
  for(const k of ['API_ID','API_HASH','BOT_TOKEN','CHANNEL','GDRIVE_CLIENT_ID','GDRIVE_CLIENT_SECRET','PHOTOS_CLIENT_ID','PHOTOS_CLIENT_SECRET']){
    const el=document.getElementById(k);
    if(el&&s[k]!=null)el.value=s[k];
  }
}
function setMsg(text,kind){
  const m=document.getElementById('msg');
  m.textContent=text;m.className='msg'+(kind?' '+kind:'');
}
async function saveSettings(e){
  e.preventDefault();
  const btn=document.getElementById('saveBtn');
  btn.disabled=true;
  setMsg('');
  const body={};
  for(const k of ['API_ID','API_HASH','BOT_TOKEN','CHANNEL','GDRIVE_CLIENT_ID','GDRIVE_CLIENT_SECRET','PHOTOS_CLIENT_ID','PHOTOS_CLIENT_SECRET']){
    const el=document.getElementById(k);
    body[k]=el.value;
  }
  const d=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(d.ok){setMsg('✅ Pengaturan tersimpan','ok');loadSettings();}
  else{setMsg('❌ '+(d.error||'Gagal'),'err');}
  btn.disabled=false;
}
async function testTelegram(){
  const btn=document.getElementById('testBtn');
  btn.disabled=true;
  setMsg('Menguji koneksi...');
  const d=await api('/api/settings/test-telegram',{method:'POST'});
  if(d.connected){setMsg('✅ Terhubung sebagai @'+(d.username||'?'),'ok');}
  else{setMsg('❌ '+(d.error||'Gagal terhubung'),'err');}
  btn.disabled=false;
}
async function restartService(){
  const btn=document.getElementById('restartBtn');
  btn.disabled=true;
  setMsg('Memulai ulang layanan...');
  const d=await api('/api/settings/restart',{method:'POST'});
  if(d.ok){setMsg('✅ Layanan sedang di-restart','ok');}
  else{setMsg('❌ '+(d.error||'Gagal restart'),'err');}
  btn.disabled=false;
}
init();
</script>
</body>
</html>'''

# Registration
# ------------------------------------------------------------------------------
def register_drive_features(flask_app, mount_prefix=""):
    """Register all profile + gdrive routes onto a Flask app (web.py's `app`).

    `mount_prefix` is accepted for forward-compatibility but the blueprint
    already uses url_prefix="" so routes register at their absolute paths.
    """
    init_drive_db()
    flask_app.register_blueprint(drive_ext, url_prefix=mount_prefix)
    return flask_app

# Convenience: if imported as part of web.py, register automatically.
if _HAS_HOST:
    try:
        register_drive_features(_host_app)
    except Exception as e:  # pragma: no cover
        print(f"[web_drive] auto-register skipped: {e}")

if __name__ == "__main__":
    # Standalone smoke test
    from flask import Flask
    test_app = Flask(__name__)
    test_app.secret_key = "test"
    init_drive_db()
    register_drive_features(test_app)
    print("Routes registered:")
    for r in test_app.url_map.iter_rules():
        if "drive_ext" in r.endpoint or "profile" in r.rule or "gdrive" in r.rule:
            print("  ", r.rule, r.methods)
    print("OK - web_drive module loads cleanly.")
