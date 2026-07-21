"""
Backup All Google Drive → Telegram
Add this to web_drive.py as a new endpoint + frontend update
"""
BACKUP_ADDITIONS = '''
# ============================================================
# BACKUP ALL GOOGLE DRIVE
# ============================================================
import threading

# Global backup progress tracker
_backup_progress = {}
_backup_lock = threading.Lock()

def _get_user_email(drive_service):
    """Get authenticated user's email from Google."""
    try:
        about = drive_service.about().get(fields="user(emailAddress)").execute()
        return about.get("user", {}).get("emailAddress", "unknown")
    except Exception:
        return "unknown"

def _list_all_files(drive_service, parent_id="root", path=""):
    """Recursively list all files/folders in Google Drive."""
    items = []
    page_token = None
    while True:
        result = drive_service.files().list(
            q=f"'{parent_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageToken=page_token,
            pageSize=100,
        ).execute()
        for f in result.get("files", []):
            f["path"] = path
            items.append(f)
            if f["mimeType"] == "application/vnd.google-apps.folder":
                sub_path = f"{path}/{f['name']}" if path else f["name"]
                items.extend(_list_all_files(drive_service, f["id"], sub_path))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items

def _export_google_doc(drive_service, file_id, mime_type, filename):
    """Export Google Docs/Sheets/Slides to downloadable format."""
    export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    }
    if mime_type in export_map:
        export_mime, ext = export_map[mime_type]
        if not filename.endswith(ext):
            filename += ext
        request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime)
        return request, filename, export_mime
    return None, filename, mime_type

def _backup_worker(user_id, folder_id):
    """Background worker to backup all Google Drive files."""
    global _backup_progress
    try:
        with _backup_lock:
            _backup_progress[user_id] = {
                "status": "starting",
                "total": 0,
                "done": 0,
                "current": "",
                "errors": [],
                "email": "",
            }

        # Get drive service
        creds = _get_creds(user_id)
        if not creds:
            with _backup_lock:
                _backup_progress[user_id]["status"] = "error"
                _backup_progress[user_id]["errors"].append("Not connected")
            return

        drive_service = build("drive", "v3", credentials=creds)

        # Get user email
        email = _get_user_email(drive_service)
        with _backup_lock:
            _backup_progress[user_id]["email"] = email
            _backup_progress[user_id]["status"] = "listing"

        # List all files recursively
        all_items = _list_all_files(drive_service)
        files = [i for i in all_items if i["mimeType"] != "application/vnd.google-apps.folder"]
        folders = [i for i in all_items if i["mimeType"] == "application/vnd.google-apps.folder"]

        with _backup_lock:
            _backup_progress[user_id]["total"] = len(files)
            _backup_progress[user_id]["status"] = "copying"

        # Create backup folder structure in Telegram
        # Root: "Backup - email@gmail.com"
        backup_root_name = f"Backup - {email}"

        # Create root backup folder
        db_exec("INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, 0)", (backup_root_name,))
        rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=0", (backup_root_name,))
        root_folder_id = rows[0]["id"] if rows else 0

        # Create subfolders
        folder_map = {}
        for fo in folders:
            sub_path = fo["path"]
            if sub_path:
                # Create nested folders
                parts = sub_path.split("/")
                parent = root_folder_id
                for part in parts:
                    existing = db_query("SELECT id FROM folders WHERE name=? AND parent_id=?", (part, parent))
                    if existing:
                        parent = existing[0]["id"]
                    else:
                        db_exec("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (part, parent))
                        new_rows = db_query("SELECT id FROM folders WHERE name=? AND parent_id=?", (part, parent))
                        parent = new_rows[0]["id"] if new_rows else 0
                folder_map[fo["id"]] = parent
            else:
                folder_map[fo["id"]] = root_folder_id

        # Copy each file
        for idx, item in enumerate(files):
            with _backup_lock:
                _backup_progress[user_id]["current"] = item["name"]
                _backup_progress[user_id]["done"] = idx

            target_folder = folder_map.get(item.get("id", ""), root_folder_id)
            # If file is inside a subfolder, use that folder
            if item.get("path"):
                parts = item["path"].split("/")
                parent = root_folder_id
                for part in parts:
                    existing = db_query("SELECT id FROM folders WHERE name=? AND parent_id=?", (part, parent))
                    if existing:
                        parent = existing[0]["id"]
                target_folder = parent

            try:
                filename = item["name"]
                mime = item["mimeType"]
                is_gdoc = mime.startswith("application/vnd.google-apps.")

                if is_gdoc:
                    request, filename, mime = _export_google_doc(drive_service, item["id"], mime, filename)
                    if not request:
                        continue
                    buf = io.BytesIO()
                    request.download(buf)
                    buf.seek(0)
                    data = buf.read()
                else:
                    # Download regular file
                    request = drive_service.files().get_media(fileId=item["id"])
                    buf = io.BytesIO()
                    downloader = MediaIoBaseBuffer(buf, mimetype=mime)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                    buf.seek(0)
                    data = buf.read()

                # Upload to Telegram
                forwarded = run_async(telethon_client.send_file(
                    CHANNEL,
                    file=data,
                    caption=f"cloud:{target_folder}:{filename}",
                    force_document=True,
                ))

                # Save to DB
                db_exec(
                    "INSERT INTO files (msg_id, file_name, file_size, mime_type, folder_id) VALUES (?, ?, ?, ?, ?)",
                    (forwarded.id, filename, len(data), mime, target_folder),
                )

            except Exception as e:
                with _backup_lock:
                    _backup_progress[user_id]["errors"].append(f"{item['name']}: {str(e)[:100]}")

        with _backup_lock:
            _backup_progress[user_id]["status"] = "done"
            _backup_progress[user_id]["done"] = len(files)

    except Exception as e:
        with _backup_lock:
            _backup_progress[user_id]["status"] = "error"
            _backup_progress[user_id]["errors"].append(str(e)[:200])


@drive_ext.route("/api/gdrive/backup-all", methods=["POST"])
def gdrive_backup_all():
    """Start backup of all Google Drive files to Telegram."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401

    with _backup_lock:
        if uid in _backup_progress and _backup_progress[uid]["status"] in ("starting", "listing", "copying"):
            return jsonify({"error": "backup already in progress"}), 409

    t = threading.Thread(target=_backup_worker, args=(uid, 0), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "backup started"})


@drive_ext.route("/api/gdrive/backup-status")
def gdrive_backup_status():
    """Get backup progress."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401

    with _backup_lock:
        progress = _backup_progress.get(uid, {"status": "idle"})
    return jsonify(progress)
'''

# These will be injected into web_drive.py
print(BACKUP_ADDITIONS)
print("BACKUP_MODULE_READY")
