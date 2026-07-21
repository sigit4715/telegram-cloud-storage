#!/usr/bin/env python3
"""
Full flow: coba session lama → create app → ambil api_id/api_hash
Kalau session expired, minta OTP baru.
"""
import re, json, http.cookiejar, urllib.parse, urllib.request, os

BASE = "https://my.telegram.org"
COOKIE_FILE = "/tmp/tg_session_cookies.txt"

cj = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
if os.path.exists(COOKIE_FILE):
    try:
        cj.load(ignore_discard=True)
        print(f"[0] Loaded {len(list(cj))} cookies")
    except:
        print("[0] Cookie corrupt, fresh start")

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
]

def post_ajax(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    resp = opener.open(req, timeout=30).read().decode()
    cj.save(COOKIE_FILE, ignore_discard=True)
    return resp

def get_page(path):
    req = urllib.request.Request(BASE + path)
    resp = opener.open(req, timeout=30).read().decode()
    cj.save(COOKIE_FILE, ignore_discard=True)
    return resp

# Step 1: Coba akses /apps langsung (session mungkin masih aktif)
print("[1] Cek session...")
try:
    html = get_page("/apps")
    if "Create new application" in html or "api_id" in html.lower():
        print("    ✅ Session masih aktif!")
    else:
        raise Exception("Not apps page")
except Exception as e:
    print(f"    Session expired ({e}), perlu login ulang...")
    print("    OTP_NEEDED")
    exit(0)

# Step 2: Cek apakah api_id sudah ada
aid = re.search(r'api_id["\']?\s*[=:]\s*["\']?(\d+)', html)
ahash = re.search(r'api_hash["\']?\s*[=:]\s*["\']([a-f0-9]+)', html)
if aid and ahash:
    print(f"    ✅ API_ID={aid.group(1)} API_HASH={ahash.group(1)}")
    with open("/opt/data/telegram-cloud/.api_credentials", "w") as f:
        f.write(f"API_ID={aid.group(1)}\nAPI_HASH={ahash.group(1)}\n")
    print("    TERSIMPAN!")
    exit(0)

# Step 3: Create app
print("[2] Create app...")
m = re.search(r'value=["\']([a-f0-9]{16,})["\']', html)
csrf = m.group(1) if m else ""
print(f"    CSRF: {csrf}")

r2 = post_ajax("/apps/create", {
    "app_title": "CloudStorage",
    "app_shortname": "cloudstore",
    "app_url": "https://example.com",
    "app_platform": "desktop",
    "app_desc": "Personal cloud storage via Telethon",
    "random_hash": csrf,
})
print(f"    Response: {r2[:200]}")

if not r2.strip() or r2.strip() == "null":
    print("    ✅ App created!")
elif "error" in r2.lower():
    print(f"    ⚠️  {r2[:100]}")

# Step 4: Ambil credentials
print("[3] GET /apps...")
html2 = get_page("/apps")
aid2 = re.search(r'api_id["\']?\s*[=:]\s*["\']?(\d+)', html2)
ahash2 = re.search(r'api_hash["\']?\s*[=:]\s*["\']([a-f0-9]+)', html2)
if aid2 and ahash2:
    print(f"    ✅ API_ID={aid2.group(1)} API_HASH={ahash2.group(1)}")
    with open("/opt/data/telegram-cloud/.api_credentials", "w") as f:
        f.write(f"API_ID={aid2.group(1)}\nAPI_HASH={ahash2.group(1)}\n")
    print("    TERSIMPAN!")
else:
    print(f"    ❌ Gagal. Cek response manual.")
