"""
Google Photos Picker API routes for web_drive.py
Add this code to web_drive.py before the Registration section
"""

PHOTOS_API_CODE = """
# ============================================================
# GOOGLE PHOTOS PICKER API
# ============================================================
import urllib.request

PHOTOS_SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly", "openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]
_picker_sessions = {}

def _client_config():
    cid = cfg.get("GDRIVE_CLIENT_ID")
    csec = cfg.get("GDRIVE_CLIENT_SECRET")
    if not cid or not csec:
        return None
    return {"web": {"client_id": cid, "client_secret": csec, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https"))]}}

def _get_photos_credentials(uid):
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
    return {"Authorization": f"Bearer {creds.token}"}

@drive_ext.route("/api/photos/status")
def photos_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    headers = _get_photos_headers(uid)
    if not headers:
        return jsonify({"connected": False})
    try:
        req = urllib.request.Request("https://photospicker.googleapis.com/v1/sessions", data=json.dumps({}).encode(), headers={**headers, "Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        sid = data.get("id")
        if sid:
            del_req = urllib.request.Request(f"https://photospicker.googleapis.com/v1/sessions/{sid}", headers=headers, method="DELETE")
            urllib.request.urlopen(del_req, timeout=5)
        return jsonify({"connected": True})
    except Exception:
        return jsonify({"connected": False})

@drive_ext.route("/api/photos/auth")
def photos_auth():
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
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent", state=state)
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
    saved_scopes = session.pop("photos_scopes", PHOTOS_SCOPES)
    flow = Flow.from_client_config(client_config, scopes=saved_scopes, redirect_uri=cfg.get("GDRIVE_REDIRECT_URI", url_for("drive_ext.gdrive_callback", _external=True, _scheme="https")), state=expected_state)
    saved_verifier = session.pop("photos_code_verifier", None)
    if saved_verifier:
        flow.code_verifier = saved_verifier
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        return jsonify({"error": f"token exchange failed: {e}"}), 400
    creds = flow.credentials
    expiry = creds.expiry.isoformat() if creds.expiry else None
    db_exec(\"\"\"INSERT INTO google_tokens (user_id, access_token, refresh_token, token_expiry)
        VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
        access_token=excluded.access_token,
        refresh_token=COALESCE(excluded.refresh_token, google_tokens.refresh_token),
        token_expiry=excluded.token_expiry\"\"\", (uid, creds.token, creds.refresh_token, expiry))
    session.pop("photos_state", None)
    session.pop("photos_uid", None)
    return redirect(url_for("drive_ext.gdrive_page"))

@drive_ext.route("/api/photos/picker/create", methods=["POST"])
def photos_picker_create():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    headers = _get_photos_headers(uid)
    if not headers:
        return jsonify({"error": "not connected to Google Photos", "connected": False}), 401
    req = urllib.request.Request("https://photospicker.googleapis.com/v1/sessions", data=json.dumps({}).encode(), headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        session_id = data.get("id")
        picker_uri = data.get("pickerUri")
        if session_id and picker_uri:
            _picker_sessions[uid] = {"session_id": session_id, "status": "pending"}
            return jsonify({"ok": True, "session_id": session_id, "picker_uri": picker_uri})
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
        url = f"https://photospicker.googleapis.com/v1/mediaItems?sessionId={ps['session_id']}&pageSize=100"
        if page_token:
            url += f"&pageToken={page_token}"
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
    # Get media items
    items = []
    url = f"https://photospicker.googleapis.com/v1/mediaItems?sessionId={ps['session_id']}&pageSize=100"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        items = data.get("mediaItems", [])
    except Exception:
        return jsonify({"error": "failed to get items"}), 500
    if item_ids:
        items = [i for i in items if i.get("id") in item_ids]
    # Create Google Photos folder if needed
    db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, 0)", ("Google Photos",))
    rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=0", ("Google Photos",))
    folder_id = rows[0]["id"] if rows else 0
    imported = 0
    errors = []
    for item in items:
        try:
            base_url = item.get("baseUrl", "")
            if not base_url:
                continue
            dl_url = f"{base_url}=d"
            dl_req = urllib.request.Request(dl_url, headers=headers)
            dl_resp = urllib.request.urlopen(dl_req, timeout=120)
            file_data = dl_resp.read()
            filename = item.get("filename", "photo.jpg")
            mime = item.get("mimeType", "image/jpeg")
            from web import telethon_client as _tc, run_async as _ra, CHANNEL as _ch
            forwarded = _ra(_tc.send_file(_ch, file=file_data, caption=f"photos:{folder_id}:{filename}", force_document=True))
            db_exec("INSERT INTO files (msg_id, file_name, size, mime, folder_id) VALUES (?, ?, ?, ?, ?)", (forwarded.id, filename, len(file_data), mime, folder_id))
            imported += 1
        except Exception as e:
            errors.append(f"{item.get('filename','?')}: {str(e)[:100]}")
    # Delete session
    try:
        del_req = urllib.request.Request(f"https://photospicker.googleapis.com/v1/sessions/{ps['session_id']}", headers=headers, method="DELETE")
        urllib.request.urlopen(del_req, timeout=10)
    except Exception:
        pass
    _picker_sessions.pop(uid, None)
    return jsonify({"ok": True, "imported": imported, "errors": errors, "total": len(items)})
"""
