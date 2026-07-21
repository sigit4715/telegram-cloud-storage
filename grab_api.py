#!/usr/bin/env python3
"""
Grab api_id + api_hash dari my.telegram.org
AJAX endpoints:
  POST /auth/send_password  {phone}              → OTP sent, returns random_hash
  POST /auth/login          {phone, password, random_hash} → login
  GET  /apps                                                → api_id + api_hash
  POST /apps              {app_title, app_shortname, ...}   → create app
"""
import sys, re, json, http.cookiejar, urllib.parse, urllib.request

BASE = "https://my.telegram.org"
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
    ("X-Requested-With", "XMLHttpRequest"),
]

def post_ajax(path, data):
    url = BASE + path
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    resp = opener.open(req, timeout=30).read().decode()
    return resp

def get_page(path):
    req = urllib.request.Request(BASE + path)
    return opener.open(req, timeout=30).read().decode()

def extract_csrf(html):
    for pat in [
        r'id=["\']my_random_hash["\'][^>]*value=["\']([^"\']+)',
        r'name=["\']random_hash["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([a-f0-9]{16,})["\']',
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
phone = sys.argv[2] if len(sys.argv) > 2 else "+6285929931919"

if cmd == "step1":
    print(f"[1] POST /auth/send_password → {phone}...")
    resp = post_ajax("/auth/send_password", {"phone": phone})
    try:
        data = json.loads(resp)
        print(f"    Response: {json.dumps(data)}")
        random_hash = data.get("random_hash", "")
        print(f"    random_hash = {random_hash}")
        if random_hash:
            # Simpan random_hash ke file
            with open("/tmp/tg_random_hash", "w") as f:
                f.write(random_hash)
            print("    ✅ OTP dikirim + random_hash disimpan!")
        else:
            print("    ⚠️  random_hash kosong, tapi mungkin tetap jalan")
    except json.JSONDecodeError:
        print(f"    Response (raw): {resp[:300]}")
        if "error" in resp.lower():
            print("    ❌ Gagal kirim OTP")
        else:
            print("    ✅ Mungkin berhasil (non-JSON response)")

elif cmd == "step2":
    otp = sys.argv[2]
    try:
        random_hash = open("/tmp/tg_random_hash").read().strip()
    except FileNotFoundError:
        random_hash = ""
    print(f"[2] POST /auth/login → OTP={otp}...")
    resp = post_ajax("/auth/login", {
        "phone": phone,
        "password": otp,
        "random_hash": random_hash,
    })
    try:
        data = json.loads(resp)
        print(f"    Response: {json.dumps(data)}")
    except json.JSONDecodeError:
        print(f"    Response: {resp[:300]}")

    if "success" in resp.lower() or "ok" in resp.lower() or resp.strip() == "true" or '"ok"' in resp:
        print("    ✅ Login berhasil!")
    else:
        print("    Response mentah, coba GET /apps langsung...")

    # Langsung cek /apps
    print("[3] GET /apps...")
    html = get_page("/apps")
    aid = re.search(r'api_id["\']?\s*[=:]\s*["\']?(\d+)', html)
    ahash = re.search(r'api_hash["\']?\s*[=:]\s*["\']([a-f0-9]+)', html)
    if aid and ahash:
        print(f"\n{'='*40}")
        print(f"✅ API_ID   = {aid.group(1)}")
        print(f"✅ API_HASH = {ahash.group(1)}")
        print(f"{'='*40}")
        with open("/opt/data/telegram-cloud/.api_credentials", "w") as f:
            f.write(f"API_ID={aid.group(1)}\nAPI_HASH={ahash.group(1)}\n")
        print("Tersimpan ke /opt/data/telegram-cloud/.api_credentials")
    else:
        if "Create new application" in html:
            print("    Belum ada app, membuat baru...")
            csrf = extract_csrf(html)
            result = post_ajax("/apps", {
                "app_title": "CloudStorage",
                "app_shortname": "cloudstore",
                "app_url": "https://example.com",
                "app_platform": "desktop",
                "app_desc": "Personal cloud storage via Telethon",
                "random_hash": csrf or "",
            })
            try:
                rdata = json.loads(result)
                aid = rdata.get("app_id") or rdata.get("App api_id")
                ahash = rdata.get("app_hash") or rdata.get("App api_hash")
            except json.JSONDecodeError:
                aid = re.search(r'api_id["\']?\s*[=:]\s*["\']?(\d+)', result)
                ahash = re.search(r'api_hash["\']?\s*[=:]\s*["\']([a-f0-9]+)', result)
                aid = aid.group(1) if aid else None
                ahash = ahash.group(1) if ahash else None
            if aid and ahash:
                print(f"\n{'='*40}")
                print(f"✅ API_ID   = {aid}")
                print(f"✅ API_HASH = {ahash}")
                print(f"{'='*40}")
                with open("/opt/data/telegram-cloud/.api_credentials", "w") as f:
                    f.write(f"API_ID={aid}\nAPI_HASH={ahash}\n")
                print("Tersimpan!")
            else:
                print(f"    ❌ Gagal. Response: {result[:400]}")
        else:
            print(f"    ❌ Halaman tidak dikenal: {html[:300]}")

else:
    print(__doc__)
