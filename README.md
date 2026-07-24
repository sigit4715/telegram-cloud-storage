# ☁️ Telegram Cloud Storage

**Penyimpanan cloud pribadi tanpa batas** yang memanfaatkan Telegram sebagai backend storage. Kelola file, folder, sinkronisasi Google Drive, dan akses semuanya dari satu dashboard web modern.

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-green?logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-3-orange?logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 🚀 Fitur Utama

### 📁 File Management
- **Upload & Download** file langsung ke Telegram (mendukung file hingga 2GB)
- **Folder Hierarchy** — buat, navigasi, dan kelola folder seperti file manager biasa
- **Multi-Upload** — upload banyak file sekaligus dengan progress bar real-time
- **Search & Pagination** — cari file dengan cepat, navigasi antar halaman
- **File Preview** — preview gambar, video, audio, PDF, teks, dan dokumen Office langsung di browser
- **Favorites** — tandai file favorit untuk akses cepat
- **Trash & Permanent Delete** — hapus file ke trash, atau hapus permanen dari Telegram

### 🔗 Google Drive Integration
- **Connect Google Drive** — hubungkan akun Google Drive via OAuth2
- **Browse & Copy** — telusuri file Google Drive dan salin ke Telegram Storage
- **Bi-directional Sync** — sinkronisasi file dari Google Drive ke Telegram secara otomatis
- **Sync Progress** — pantau progress sinkronisasi real-time dengan retry mechanism
- **Disconnect** — putuskan koneksi Google Drive kapan saja

### 📸 Google Photos Integration
- **Browse Photos** — akses Google Photos langsung dari sidebar
- **Embed Panel** — tampilan Google Photos terintegrasi di dalam dashboard

### 👤 User Profile & Multi-Account
- **User Profile** — atur nama, email, telepon, bio, dan foto profil
- **Auto-Populate dari Google** — profil otomatis terisi dari data Google OAuth
- **Multi-Account Isolation** — setiap akun Google memiliki penyimpanan terisolasi
- **Admin Panel** — panel pengaturan untuk admin (Google Client ID/Secret)

### ⚙️ Telegram Configuration
- **API Settings** — atur API ID, API Hash, Bot Token, dan Channel ID
- **Channel Target Normalization** — otomatis menormalkan format Channel ID Telegram
- **Connection Test** — uji koneksi ke Telegram langsung dari dashboard
- **Multi-Account Support** — dukungan multiple Telegram bot accounts

### 🎨 UI/UX
- **Dark Mode** — tema gelap modern dengan aksen ungu
- **Responsive Design** — tampilan optimal di desktop, tablet, dan mobile
- **Mobile Bottom Navigation** — navigasi mudah di perangkat mobile
- **Inline Settings Panel** — pengaturan terpadu tanpa pindah halaman
- **Toast Notifications** — notifikasi status upload/download/sync

---

## 🏗️ Arsitektur

```
telegram-cloud-storage/
├── web.py                 # Main Flask app + Telegram integration (29 routes)
├── web_drive.py           # Google Drive/Photos + Profile features (37 routes)
├── telegram_accounts.py   # Multi-account Telegram management
├── storage.db             # SQLite database (auto-created)
├── config.env             # Environment configuration
├── profiles/              # User profile photos
├── icons/                 # SVG icon set
│   ├── cloud-storage-icons/
│   └── cloud-storage-svg-complete/
└── test_*.py              # Unit & integration tests (15 test files)
```

### Tech Stack

| Komponen | Teknologi |
|----------|-----------|
| **Backend** | Python 3.8+, Flask 3.x |
| **Database** | SQLite3 (file-based) |
| **Telegram** | Telethon (MTProto API) |
| **Google API** | Google Drive API v3, Google Photos API |
| **Frontend** | Vanilla JavaScript (ES5), HTML5, CSS3 |
| **Icons** | Custom SVG icon set |
| **Deployment** | systemd service, Cloudflare Tunnel |

---

## 📦 Instalasi

### Prerequisites
- Python 3.8 atau lebih baru
- Telegram API credentials (dari [my.telegram.org](https://my.telegram.org))
- Google Cloud Project (untuk fitur Google Drive/Photos)

### 1. Clone Repository

```bash
git clone https://github.com/sigit4715/telegram-cloud-storage.git
cd telegram-cloud-storage
```

### 2. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask telethon pillow google-api-python-client google-auth-oauthlib
```

### 3. Konfigurasi

Buat file `config.env`:

```env
# Telegram Settings
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx

# Google OAuth (opsional)
GDRIVE_CLIENT_ID=your_client_id.apps.googleusercontent.com
GDRIVE_CLIENT_SECRET=GOCSPX-your_secret
PHOTOS_CLIENT_ID=your_client_id.apps.googleusercontent.com
PHOTOS_CLIENT_SECRET=GOCSPX-your_secret

# Admin (opsional)
ADMIN_IDS=your_google_email@gmail.com
```

### 4. Jalankan

```bash
python3 web.py
```

Server akan berjalan di `http://localhost:8050`

---

## 🔧 Deployment (Production)

### Systemd Service

```bash
sudo tee /etc/systemd/system/telegram-cloud-web.service << 'EOF'
[Unit]
Description=Telegram Cloud Storage Web
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/data/telegram-cloud
ExecStart=/opt/data/.venv-scrapling/bin/python3 web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable telegram-cloud-web
sudo systemctl start telegram-cloud-web
```

### Cloudflare Tunnel (Optional)

Untuk akses publik tanpa port forwarding:

```bash
cloudflared tunnel --url http://localhost:8050
```

---

## 🧪 Testing

Jalankan semua unit test:

```bash
python3 -m unittest discover -p "test_*.py" -v
```

### Test Coverage

| Test File | Deskripsi |
|-----------|-----------|
| `test_login_redirect.py` | Login & redirect behavior |
| `test_telegram_settings_page.py` | Settings page rendering |
| `test_telegram_channel_target.py` | Channel ID normalization |
| `test_telegram_account_config.py` | Multi-account configuration |
| `test_multi_account_isolation.py` | Data isolation per account |
| `test_web_drive_host_binding.py` | Drive blueprint registration |
| `test_web_main_alias.py` | Module import safety |
| `test_file_preview.py` | Preview functionality |
| + 7 UI tests | Pagination, stat cards, file types, etc. |

---

## 📱 Screenshots

### Dashboard
- Statistik penyimpanan (File, Ukuran, Folder, Foto, Video)
- Daftar file terbaru dengan pagination
- Sidebar navigasi dengan mode gelap

### Settings Panel (Inline)
- Tab **Profil** — atur data personal & foto profil
- Tab **Telegram** — konfigurasi API & Channel
- Tab **Google** — kredensial OAuth (admin only)
- Tab **Tutorial** — panduan setup Telegram

### Google Drive
- Status koneksi (Terhubung / Tidak terhubung)
- Browser file Google Drive
- Tombol Copy to Telegram & Sync

---

## 🔒 Keamanan

- **Session-based Authentication** — login via ID atau Google OAuth
- **Multi-Tenant Isolation** — data per akun terisolasi di database
- **CSRF Protection** — session token untuk semua request
- **No Secrets in Code** — credentials disimpan di `config.env` (tidak di-commit)
- **HTTPS via Cloudflare** — enkripsi data saat transit

---

## 📄 API Endpoints

### Core (web.py)

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| POST | `/api/login` | Login dengan User ID |
| POST | `/api/logout` | Logout |
| GET | `/api/me` | Info user saat ini |
| GET | `/api/files` | List files dengan pagination |
| POST | `/api/upload` | Upload file ke Telegram |
| GET | `/api/download/<id>` | Download file |
| GET | `/api/stream/<id>` | Stream file (preview) |
| DELETE | `/api/delete/<id>` | Hapus file |
| GET | `/api/stats` | Statistik penyimpanan |
| GET | `/api/folders` | List folders |

### Google Drive & Profile (web_drive.py)

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| GET/POST | `/api/profile` | Get/update profil |
| POST | `/api/profile/photo` | Upload foto profil |
| GET | `/api/gdrive/status` | Status koneksi Google Drive |
| GET | `/api/gdrive/auth` | Mulai OAuth Google Drive |
| GET | `/api/gdrive/files` | Browse file Google Drive |
| POST | `/api/gdrive/copy` | Copy file dari GDrive ke Telegram |
| POST | `/api/gdrive/sync` | Mulai sinkronisasi |
| GET | `/api/settings` | Get settings (admin) |
| POST | `/api/settings` | Update settings (admin) |
| POST | `/api/settings/restart` | Restart service (admin) |

---

## 🤝 Contributing

1. Fork repository
2. Buat branch baru (`git checkout -b feature/amazing-feature`)
3. Commit perubahan (`git commit -m 'Add amazing feature'`)
4. Push ke branch (`git push origin feature/amazing-feature`)
5. Buka Pull Request

---

## 📝 Changelog

### v2.1.0 (Latest)
- ✅ Settings panel terpadu (Profil, Telegram, Google, Tutorial)
- ✅ Auto-populate profil dari Google OAuth
- ✅ Fixed Google Drive & Settings panel overlap
- ✅ ES5 JavaScript untuk kompatibilitas maksimal
- ✅ 15 unit test baru

### v2.0.0
- ✅ Google Drive & Photos integration
- ✅ Multi-account isolation
- ✅ File preview (image, video, PDF, Office)
- ✅ Sync progress & retry mechanism

### v1.0.0
- ✅ Initial release — Telegram Cloud Storage

---

## 📜 License

MIT License - Silakan gunakan untuk keperluan pribadi maupun komersial.

---

## 👨‍💻 Author

**sigit4715** - [GitHub](https://github.com/sigit4715)

Built with ❤️ for unlimited cloud storage using Telegram as backend.

---

## 🔗 Links

- **GitHub**: [sigit4715/telegram-cloud-storage](https://github.com/sigit4715/telegram-cloud-storage)
- **Issues**: [GitHub Issues](https://github.com/sigit4715/telegram-cloud-storage/issues)
- **Telegram API**: [my.telegram.org](https://my.telegram.org)
- **Google Cloud Console**: [console.cloud.google.com](https://console.cloud.google.com)

---

**⭐ Star this repo if you find it useful!**
