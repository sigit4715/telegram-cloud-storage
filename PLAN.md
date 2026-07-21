# Implementation Plan — Profile Page & Google Drive Integration

**Target app:** `/opt/data/telegram-cloud/web.py` (Flask + Telethon), port 8050
**Server:** 192.168.2.63 — Domain `cloud.aleca.my.id` (Cloudflare Tunnel)
**Auth model today:** Flask `session["user_id"]` (Telegram ID); `ALLOWED` set + `ADMIN_IDS={"5337119189"}`
**DB today:** SQLite `storage.db`, helpers `db_query/db_exec/db_scalar`, `init_db()`
**Theme:** Dark purple (`--bg:#0d0221; --accent:#a78bfa`)

---

## 0. Current-Code Observations (read before implementing)

These affect both features and should be fixed as part of the work:

1. **`secret_key` regenerates on every restart** (`app.secret_key = os.urandom(24)`). This logs every user out on deploy and breaks OAuth `state` validation. → Set a **fixed** secret from `config.env` (e.g. `SESSION_SECRET`). Note: the task brief mentions a fixed key `'telegram-cloud-storage-2026'`; reconcile — prefer a config.env value, fall back to the literal if absent.
2. **No CSRF protection** on POST endpoints. Add a simple same-origin origin/referer check or a session CSRF token for all state-changing routes (login, profile save, Drive copy, OAuth callback).
3. **Telethon is a single global client** on one background loop; Drive copy will contend for it. Use the existing `run_async()` bridge and **queue copies** (one at a time) to respect FloodWait.
4. **No auth on Google OAuth callback** beyond the `state` cookie — must tie the returned refresh token to `session["user_id"]`, never to a global.
5. **`requirements.txt` only has `telethon`.** Google libs must be added and installed into `/opt/data/.venv-scrapling` (the venv the systemd unit uses).

---

## 1. FEATURE A: PROFILE PAGE

### 1.1 Database schema change
Add to `init_db()` (new table, idempotent):

```sql
CREATE TABLE IF NOT EXISTS user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    name TEXT,
    email TEXT,
    bio TEXT,
    phone TEXT,
    photo_url TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
```

- `user_id` = Telegram ID string (matches `session["user_id"]` and the `ALLOWED`/`ADMIN_IDS` universe). `UNIQUE` so each user has exactly one row.
- `photo_url`: either a `/api/profile/photo/<user_id>` route (served from a local `profiles/` dir) **or** a Telegram `msg_id` reference. See 1.4.

### 1.2 New API endpoints

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/api/profile` | Return current user's profile (or empty defaults) | `login_required` |
| `PUT`/`POST` | `/api/profile` | Save name/email/bio/phone (+ optional photo) | `login_required` + CSRF |
| `GET` | `/api/profile/photo/<user_id>` | Serve profile photo bytes | public (or login_required if privacy desired) |
| `POST` | `/api/profile/photo` | Upload photo (multipart `photo`) → store, return `photo_url` | `login_required` + CSRF |

**`/api/profile` (GET)** returns:
```json
{"user_id":"5337119189","name":"","email":"","bio":"","phone":"","photo_url":"","updated_at":""}
```
If no row exists, return empty defaults (don't 404).

**`/api/profile` (POST)** — upsert pattern:
```python
db_exec(
  """INSERT INTO user_profiles (user_id,name,email,bio,phone,updated_at)
     VALUES (?,?,?,?,?,datetime('now','localtime'))
     ON CONFLICT(user_id) DO UPDATE SET
       name=excluded.name, email=excluded.email, bio=excluded.bio,
       phone=excluded.phone, updated_at=datetime('now','localtime')""",
  (uid, name, email, bio, phone))
```
Validate: `email` shape (basic regex), `phone` digits, `bio` length cap (e.g. 500), `name` cap (100). Return `{"ok":true,"updated_at":...}`.

**Photo storage decision (1.4):** store photo in the **Telegram channel** (consistent with how files are stored) OR locally. Recommendation: **local `profiles/` dir** for simplicity/speed of serving, with a `msg_id` option as fallback. `photo_url` = relative path `/api/profile/photo/<user_id>`.

### 1.3 Frontend — Profile page + component
- Add route `@app.route("/profile")` → returns `PROFILE_HTML` (mirror `DRIVE_HTML` theme/CSS vars).
- Add a **"👤 Profile"** button in the `DRIVE_HTML` topbar (`.right` div) linking to `/profile`.
- `PROFILE_HTML` structure:
  - Avatar circle (shows current photo or initials fallback) + "Change photo" input (`accept="image/*"`, preview on change).
  - Form fields: Name, Email, Phone, Bio (textarea).
  - Save button → `fetch('/api/profile',{method:'POST',...})`.
  - On load: `GET /api/profile`, populate fields + avatar.
  - Reuse `doLogout()` pattern and dark-purple CSS variables.
- Photo upload: on file select, `FormData` → `POST /api/profile/photo`; on success refresh avatar `src`.

### 1.4 Security considerations (Profile)
- Server-side validation of all text fields + `content_type`/size limit on photo (max ~5 MB, image only).
- Sanitize `photo_url` output; never allow user-supplied absolute URLs to be echoed into `<img src>` without allow-listing the `/api/profile/photo/` prefix.
- `user_id` is taken from **session**, never from request body — prevent one user editing another's profile (the upsert keys on `session["user_id"]`, ignoring any `user_id` in JSON).
- Store uploaded photo with a randomized filename (e.g. `sha256(uid+salt)` or `uuid4`) inside `profiles/`; set safe permissions; serve with `X-Content-Type-Options: nosniff` and correct `Content-Type`.

---

## 2. FEATURE B: GOOGLE DRIVE INTEGRATION

### 2.1 Google API libraries & install
Add to `requirements.txt`:
```
google-api-python-client
google-auth-oauthlib
google-auth
```
Install into the runtime venv (the systemd unit uses `/opt/data/.venv-scrapling/bin/python3`):
```bash
/opt/data/.venv-scrapling/bin/python3 -m pip install google-api-python-client google-auth-oauthlib google-auth
```
> If `pip` is unavailable in that venv, use `uv pip install --python /opt/data/.venv-scrapling/bin/python3 ...` (uv is present on host).

### 2.2 Google OAuth2 setup steps (console + code)

**Google Cloud Console:**
1. Go to https://console.cloud.google.com/ → create/select project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **OAuth consent screen** → External (or Internal if Workspace), add app name, user support email, developer contact; add scope `https://www.googleapis.com/auth/drive.readonly`; add test users (the `ALLOWED` Telegram IDs aren't Google IDs — just your own Google account(s) for testing).
4. **Credentials → Create Credentials → OAuth client ID** → Application type **Web application**.
5. **Authorized redirect URIs** add:
   - `https://cloud.aleca.my.id/api/drive/oauth/callback` (production, through Cloudflare Tunnel)
   - `http://192.168.2.63:8050/api/drive/oauth/callback` (LAN testing)
6. Download **client_secret.json** → place at `/opt/data/telegram-cloud/client_secret.json` (git-ignored, chmod 600, owned by root).
7. Add `DRIVE_CLIENT_SECRET=/opt/data/telegram-cloud/client_secret.json` to `config.env` (path configurable; default to that file).

**Scopes:** use **read-only** `https://www.googleapis.com/auth/drive.readonly` — we only copy *out* of Drive, never write. This minimizes risk and consent friction.

### 2.3 Token storage (per-user, in SQLite)
Add to `init_db()`:
```sql
CREATE TABLE IF NOT EXISTS drive_tokens (
    user_id TEXT PRIMARY KEY,
    refresh_token TEXT,
    access_token TEXT,
    token_expiry TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
```
- Store `refresh_token` (the long-lived credential) encrypted at rest — see §2.7. At minimum, restrict DB file perms (already root-only via systemd `User=root`).
- Never store tokens in session or client-side.

### 2.4 New API endpoints (Drive)

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/api/drive/status` | Is current user connected? (has refresh token) | `login_required` |
| `GET` | `/api/drive/oauth/start` | Begin OAuth: build authorization URL, set `state` cookie, redirect | `login_required` + CSRF/state |
| `GET` | `/api/drive/oauth/callback` | Exchange code → tokens, store for `session["user_id"]`, clear state | state-validated |
| `POST` | `/api/drive/disconnect` | Delete stored token for user | `login_required` |
| `GET` | `/api/drive/list?folder_id=<gdrive_id>&page=` | List files/folders in a Drive folder (root = `root`) | `login_required` + token |
| `GET` | `/api/drive/file/<file_id>/meta` | Get name/size/mime of a Drive file | `login_required` + token |
| `POST` | `/api/drive/copy` | Body `{file_ids:[...], folder_id:<local>}`. Queue download-from-Drive → upload-to-Telegram | `login_required` + token + CSRF |

**OAuth flow detail (`/api/drive/oauth/start`):**
```python
from google_auth_oauthlib.flow import Flow
flow = Flow.from_client_secrets_file(SECRET_PATH, scopes=[DRIVE_SCOPE],
        redirect_uri=CALLBACK_URL)
auth_url, state = flow.authorization_url(access_type="offline",
        include_granted_scopes="true", prompt="consent")  # prompt=consent to always get refresh token
session["oauth_state"] = state
return redirect(auth_url)
```
**Callback:**
```python
flow = Flow.from_client_secrets_file(SECRET_PATH, scopes=[DRIVE_SCOPE], redirect_uri=CALLBACK_URL)
flow.fetch_token(authorization_response=request.url)
creds = flow.credentials
# store refresh_token (may be None on subsequent consents → keep existing)
db_exec("INSERT INTO drive_tokens (user_id,refresh_token,access_token,token_expiry,updated_at) "
        "VALUES (?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "refresh_token=COALESCE(excluded.refresh_token, drive_tokens.refresh_token), "
        "access_token=excluded.access_token, token_expiry=excluded.token_expiry, "
        "updated_at=datetime('now','localtime')",
        (uid, creds.refresh_token, creds.token, iso(creds.expiry)))
```
- `CALLBACK_URL` must match the redirect URI used (choose based on `request.host` — LAN vs domain).
- **Validate `state`** against `session["oauth_state"]` before fetching token; reject mismatch.

**Building a live credentials object for API calls:**
```python
def get_drive_creds(uid):
    row = db_query("SELECT refresh_token,access_token,token_expiry FROM drive_tokens WHERE user_id=?", (uid,))
    if not row: return None
    creds = Credentials(token=row[0]["access_token"], refresh_token=row[0]["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=..., client_secret=..., scopes=[DRIVE_SCOPE])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request());  # save new access_token back to DB
    return creds

def get_drive_service(uid):
    return build("drive", "v3", credentials=get_drive_creds(uid))
```

### 2.5 Copy logic (download from Drive → upload to Telegram)
`/api/drive/copy` handler:
1. Load creds; `service = build(...)`.
2. For each `file_id`:
   - `meta = service.files().get(fileId=file_id, fields="name,size,mimeType").execute()`
   - **Skip Google-docs native types** (`application/vnd.google-apps.*`) unless exported — these aren't direct binary. For docs/sheets/slides, use `files().export()` to pdf; for others, `files().get_media()`.
   - Stream download into memory (`io.BytesIO`) or temp file for large files.
   - `run_async(telethon_client.send_file(CHANNEL, file=buf, caption=f"cloud:{local_folder_id}:{name}", force_document=True))` — **reuse the exact upload path** used by `/api/upload` so it lands in the same `files` table.
   - Insert `files` row (same columns as `/api/upload`).
   - Respect **FloodWait**: process sequentially; on `FloodWaitError`, sleep `e.seconds` (cap/queue if huge).
3. Return `{"results":[{"name","size","ok"/"error"}...]}`.

**Frontend (`DRIVE_HTML` additions / new `/drive-import` page):**
- "📥 Import from Drive" button in toolbar (or a dedicated `/drive-import` route reusing theme).
- If not connected → "Connect Google Drive" button → `GET /api/drive/oauth/start` (redirects to Google).
- If connected → folder tree browser (`/api/drive/list`), checkboxes per file, "Copy to Cloud" → `POST /api/drive/copy` with selected ids + target local `folder_id` (dropdown of existing folders).
- Show progress (reuse the existing `.upload-panel`/`.upload-item` styles), then `loadAll()` to refresh the main grid.

### 2.6 Security considerations (Drive)
- **Read-only scope** (`drive.readonly`) — app never writes to the user's Drive.
- **Per-user tokens**: token row keyed by `session["user_id"]`; a user can only act on their own Drive. Never expose tokens via API responses.
- **State/CSRF** on OAuth start + callback; reject mismatched/ missing state. Bind `state` to the session, not a global.
- **Redirect URI validation**: use the exact registered URI; derive `redirect_uri` from `request.url_root`/host but **allow-list** only the two known hosts (domain + LAN IP) to prevent open-redirect / token leakage.
- **client_secret.json**: chmod 600, root-owned, never served, git-ignored.
- **Native Google file types**: handle export-to-PDF; do not blindly stream `application/vnd.google-apps.*` (would 500 / produce garbage).
- **Token encryption at rest** (§2.7) strongly recommended since `drive_tokens` holds a long-lived refresh token.
- **Size/rate limits**: cap single-file size (e.g. 2 GB, Telethon's practical limit ~2 GB) and total batch; surface FloodWait to the user instead of failing silently.
- **Logout** (`/api/logout`) should NOT delete Drive tokens (user stays connected across sessions) — but offer explicit `/api/drive/disconnect`.

### 2.7 Optional hardening: token encryption
Wrap refresh/access tokens with `cryptography.Fernet` using a key from `config.env` (`TOKEN_ENCRYPTION_KEY`). Encrypt before `db_exec`, decrypt in `get_drive_creds`. Add `cryptography` to requirements. Keeps long-lived credentials safe even if DB is copied.

---

## 3. FILE-BY-FILE CHANGE SUMMARY

**`web.py`**
- `init_db()`: add `user_profiles` + `drive_tokens` tables.
- `config.env` load: add `SESSION_SECRET`, `DRIVE_CLIENT_SECRET`, optional `TOKEN_ENCRYPTION_KEY`.
- Set `app.secret_key` from `SESSION_SECRET` (fallback to literal `'telegram-cloud-storage-2026'`).
- Profile: 3–4 API routes + `PROFILE_HTML` + `/profile` route + topbar link in `DRIVE_HTML`.
- Drive: 7 API routes + OAuth helper funcs (`get_drive_creds`, `get_drive_service`) + `/drive-import` page (or embedded panel) + import button in `DRIVE_HTML`.
- Add a shared CSRF/origin check decorator applied to all POST/PUT routes.

**`config.env` / `config.env.example`**
- Add `SESSION_SECRET=`, `DRIVE_CLIENT_SECRET=/opt/data/telegram-cloud/client_secret.json`, `TOKEN_ENCRYPTION_KEY=` (optional).

**`requirements.txt`**
- Add `google-api-python-client`, `google-auth-oauthlib`, `google-auth`, (optional `cryptography`).

**New files**
- `client_secret.json` (from Google Console, chmod 600, git-ignored) — NOT committed.
- `profiles/` directory (for local profile photos, git-ignored).
- Add `client_secret.json` and `profiles/` to `.gitignore` (if repo is git-tracked).

**`telegram-cloud-web.service`** — no change needed (same ExecStart venv); just ensure venv has new packages.

---

## 4. DEPLOY / VERIFY CHECKLIST
1. `pip install` the new Google packages into `/opt/data/.venv-scrapling`.
2. Drop `client_secret.json`; set perms 600; add `DRIVE_CLIENT_SECRET` to `config.env`.
3. Set a fixed `SESSION_SECRET` in `config.env`.
4. `sudo systemctl daemon-reload && sudo systemctl restart telegram-cloud-web.service`.
5. Verify: login → /profile save + photo upload + reload persists.
6. Verify: /drive-import → Connect → Google consent → list files → copy one file → appears in main grid (msg_id in Telegram channel).

## 5. RISKS / OPEN QUESTIONS
- Telethon FloodWait during large Drive copies → implement a background queue + progress polling instead of blocking the request (recommended for batches > a few files).
- Native Google Workspace files need export handling (docs/sheets/slides/drawings).
- The `user_id` in this app is a Telegram ID; Google account linkage is implicit (whoever is logged in connects their own Google account via OAuth) — confirm that's the intended model.
- Large files: Telethon upload is memory-heavy; stream to a temp file for >50 MB.
