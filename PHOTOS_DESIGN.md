# Google Photos Integration — Design Plan (`/photos`)

**Project:** Telegram Cloud Storage Web Dashboard
**Server:** `192.168.2.63` · Live: `cloud.aleca.my.id`
**Audience:** Indonesian users (UI copy in Bahasa Indonesia)
**Constraint:** Inline CSS + JS only. **No frameworks, no glassmorphism.** Dark purple theme matching the existing dashboard.

---

## 0. Design Principles (must match existing dashboard)

The page must feel *native* to the dashboard. Every class, color, and JS helper below is derived from the two existing templates (`DRIVE_HTML` in `web.py`, `GDRIVE_HTML`/`PROFILE_HTML` in `web_drive.py`).

Reuse the established conventions:
- `api(url, opts)` helper → `fetch(url,{credentials:'same-origin',...opts}).then(r=>r.json())`
- `init()` runs on load: check `/api/me`, redirect to `/` if not logged in, then load page state.
- Connection badge uses the `.status-dot` / `.status-dot.on` pattern (green = connected, red = disconnected) — same as `GDRIVE_HTML`.
- Import progress reuses the `.progress-panel` / `.pitem` pattern — same as `GDRIVE_HTML` copy progress.
- Thumbnail grid reuses `.files` / `.file` / `.thumb` grid pattern — same as `DRIVE_HTML`.
- Back button reuses the `.back` anchor (`href="/drive"`) — same as `PROFILE_HTML` & `GDRIVE_HTML`.
- Google Photos Picker is **session-based**: there is no file list to browse, the picker is opened in a popup and returns selected items. This differs from Drive (which browses folders), so the UI centers on **Connect → Pick → Preview → Import**.

**Anti-glassmorphism rules:** No `backdrop-filter`, no translucent blurred panels, no semi-transparent white overlays. All surfaces are solid `#1a0a3e` (card) on `#0d0221` (bg) with a solid `1px #6c3baa` border. The only transparency allowed is the modal scrim (`rgba(0,0,0,.8)`) and subtle `rgba(167,139,250,.X)` hover tints already used in the dashboard.

---

## 1. Exact CSS Variables

Place verbatim in `:root`. These are **identical** to the existing `--bg/--card/--border/--accent/...` tokens so the page is indistinguishable from the rest of the app:

```css
:root{
  --bg:#0d0221;
  --card:#1a0a3e;
  --border:#6c3baa;
  --accent:#a78bfa;
  --accent2:#8b5cf6;
  --text:#e0e0e0;
  --muted:#6b7280;
  --danger:#f87171;
  --green:#34d399;
  --yellow:#fbbf24;
  /* photos-specific extensions (same family, no new hue) */
  --accent-grad:linear-gradient(135deg,#6c3baa,#a78bfa);
  --hover:rgba(167,139,250,.06);
  --sel:rgba(167,139,250,.12);
}
```

Global reset (verbatim from dashboard):
```css
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh}
```

---

## 2. Layout Structure

Single centered column, max-width `1000px`, matching `GDRIVE_HTML` `.wrap`.

```
┌─────────────────────────────────────────────┐
│  TOPBAR  ☁️ Google Photos        [Logout]    │  (sticky)
├─────────────────────────────────────────────┤
│  .wrap (max-width:1000px, margin:24px auto)  │
│   ┌───────────────────────────────────────┐ │
│   │ .card                                   │ │
│   │  ┌─ STATUS BAR ──────────────────────┐ │ │
│   │  │ ● (dot)  "Terhubung"  [Putuskan]  │ │ │  ← reuse .status-bar/.status-dot
│   │  └───────────────────────────────────┘ │ │
│   │                                          │ │
│   │  ┌─ CONNECT PANEL (hidden if connected)┐│ │
│   │  │ 📷 empty state + [Hubungkan] button ││ │
│   │  └─────────────────────────────────────┘│ │
│   │                                          │ │
│   │  ┌─ PICKER / IMPORT PANEL ────────────┐ │ │  (shown when connected)
│   │  │ [🖼 Buka Google Photos Picker]     │ │ │
│   │  │ [✅ Import ke Telegram (N)]         │ │ │  (disabled until items picked)
│   │  └───────────────────────────────────┘ │ │
│   │                                          │ │
│   │  ┌─ SELECTED PREVIEW GRID ───────────┐  │ │  (.files/.file/.thumb)
│   │  │ thumbnail · thumbnail · thumbnail  │  │ │
│   │  └───────────────────────────────────┘  │ │
│   │                                          │ │
│   │  ┌─ IMPORT PROGRESS PANEL ───────────┐  │ │  (.progress-panel/.pitem)
│   │  │ ✅ nama.jpg   ·  ❌ lain.jpg       │  │ │
│   │  │ Totals: ✅ 3  ❌ 1                 │  │ │
│   │  └───────────────────────────────────┘  │ │
│   │                                          │ │
│   │  ┌─ HISTORY GRID ───────────────────┐   │ │  (.files/.file/.thumb)
│   │  │ imported photo · imported photo   │   │ │
│   │  └───────────────────────────────────┘  │ │
│   │                                          │ │
│   │  .help (setup steps)                     │ │
│   │  ← Kembali ke Drive (.back)              │ │
│   └───────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

### 2.1 HTML skeleton

```html
<div class="topbar">
  <h1>☁️ Google Photos</h1>
  <div class="right"><button onclick="doLogout()">Logout</button></div>
</div>

<div class="wrap">
  <div class="card">

    <!-- STATUS BAR -->
    <div class="status-bar">
      <span class="status-dot" id="statusDot"></span>
      <span class="status-text" id="statusText">Memeriksa koneksi…</span>
      <button class="btn ghost" id="connectBtn" style="display:none" onclick="connectPhotos()">🔗 Hubungkan Google Photos</button>
      <button class="btn ghost" id="disconnectBtn" style="display:none" onclick="disconnectPhotos()">⏏ Putuskan</button>
    </div>

    <!-- CONNECT (empty) STATE -->
    <div class="empty" id="connectEmpty" style="display:none">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M21 15v4a2 2 0 01-2 2H5a2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      <p>Belum terhubung ke Google Photos.</p>
      <p style="margin-top:6px">Klik "Hubungkan Google Photos" untuk memulai.</p>
    </div>

    <!-- PICKER / IMPORT CONTROLS -->
    <div id="pickerSection" style="display:none">
      <div class="toolbar">
        <button class="btn" id="openPickerBtn" onclick="openPicker()">🖼 Buka Google Photos Picker</button>
        <button class="btn" id="importBtn" onclick="importSelected()" disabled>✅ Import ke Telegram (<span id="selCount">0</span>)</button>
        <button class="btn ghost" id="clearBtn" onclick="clearSelection()" disabled>🗑 Bersihkan</button>
      </div>
    </div>

    <!-- SELECTED PREVIEW -->
    <h3 class="section-title" id="previewTitle" style="display:none">📌 Foto Terpilih</h3>
    <div class="files" id="previewGrid"></div>

    <!-- IMPORT PROGRESS -->
    <div class="progress-panel" id="progressPanel">
      <h3>📤 Import Progress</h3>
      <div id="progressList"></div>
      <div class="totals" id="importTotals"></div>
    </div>

    <!-- HISTORY -->
    <h3 class="section-title" id="historyTitle">🕑 Riwayat Import</h3>
    <div class="files" id="historyGrid"></div>
    <div class="empty" id="historyEmpty" style="display:none">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>
      <p>Belum ada foto diimpor.</p>
    </div>

    <div class="help">
      <strong>Setup Google Cloud:</strong><br>
      1. Buat project di <code>console.cloud.google.com</code><br>
      2. Enable <code>Google Photos Library API</code> & <code>Google Photos Picker API</code><br>
      3. Credentials → OAuth client ID (Web application) → add redirect <code>/api/photos/callback</code><br>
      4. Masukkan <code>PHOTOS_CLIENT_ID</code> & <code>PHOTOS_CLIENT_SECRET</code> ke <code>config.env</code><br>
      5. Tambahkan akun Google Anda sebagai Test User di OAuth consent screen
    </div>

    <a class="back" href="/drive">← Kembali ke Drive</a>
  </div>
</div>
```

---

## 3. CSS (full component set, no glassmorphism)

```css
/* ---- topbar (identical to GDrive) ---- */
.topbar{background:#1a0a3e;border-bottom:1px solid var(--border);padding:12px 24px;
        display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent)}
.topbar .right{display:flex;align-items:center;gap:12px}
.topbar .right button{padding:6px 14px;background:transparent;border:1px solid var(--border);
        border-radius:8px;color:var(--accent);font-size:13px;cursor:pointer}

/* ---- wrap / card (identical family) ---- */
.wrap{max-width:1000px;margin:24px auto;padding:0 24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px}

/* ---- status bar (reuse GDrive pattern) ---- */
.status-bar{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.status-dot{width:10px;height:10px;border-radius:50%;background:var(--danger)}
.status-dot.on{background:var(--green)}
.status-text{font-size:13px;color:var(--muted)}

/* ---- buttons (identical to GDrive) ---- */
.btn{padding:10px 18px;background:linear-gradient(135deg,#6c3baa,#a78bfa);color:#fff;border:none;
     border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--accent)}

/* ---- toolbar / section title ---- */
.toolbar{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}
.section-title{font-size:14px;color:var(--accent);margin:20px 0 12px}

/* ---- empty state (reuse Drive svg sizing) ---- */
.empty{text-align:center;padding:40px 20px;color:var(--muted)}
.empty svg{width:48px;height:48px;stroke:var(--border);margin-bottom:8px}

/* ---- thumbnail grid (reuse DRIVE_HTML .files/.file/.thumb) ---- */
.files{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.file{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;
      cursor:pointer;transition:.2s;position:relative}
.file:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 16px rgba(108,59,170,.3)}
.file .thumb{width:100%;height:120px;background:#0d0221;display:flex;align-items:center;
      justify-content:center;overflow:hidden}
.file .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.file .info{padding:8px}
.file .info .name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file .info .meta{font-size:10px;color:var(--muted);margin-top:3px}
.file .badge{position:absolute;top:6px;left:6px;background:var(--accent);color:#fff;
      font-size:10px;padding:2px 6px;border-radius:6px}
.file .rm{position:absolute;top:6px;right:6px;width:26px;height:26px;border-radius:6px;
      border:none;background:rgba(0,0,0,.6);color:#fff;cursor:pointer;font-size:12px;
      display:none;align-items:center;justify-content:center}
.file:hover .rm{display:flex}

/* ---- progress panel (reuse GDrive pattern) ---- */
.progress-panel{display:none;margin-top:16px;background:#0d0221;border:1px solid var(--border);
      border-radius:12px;padding:16px;max-height:300px;overflow-y:auto}
.progress-panel.show{display:block}
.progress-panel h3{font-size:14px;color:var(--accent);margin-bottom:12px}
.pitem{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px}
.pitem .pname{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pitem .pstatus{font-size:11px;flex-shrink:0}
.pstatus.ok{color:var(--green)} .pstatus.err{color:var(--danger)} .pstatus.run{color:var(--accent)}
.totals{margin-top:10px;font-size:13px;color:var(--muted);border-top:1px solid rgba(108,59,170,.2);padding-top:10px}
.totals b.ok{color:var(--green)} .totals b.err{color:var(--danger)}

/* ---- help / back (reuse) ---- */
.help{margin-top:20px;font-size:12px;color:var(--muted);line-height:1.6}
.help code{background:#0d0221;padding:2px 6px;border-radius:4px;color:var(--accent)}
.back{display:inline-block;margin-top:16px;color:var(--accent);text-decoration:none;font-size:13px}

/* ---- modal (reuse Drive pattern, solid card bg) ---- */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:200;
      align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-content{background:var(--card);border:1px solid var(--border);border-radius:16px;
      padding:24px;max-width:90vw;max-height:90vh;position:relative;min-width:300px}
.modal-content img{max-width:100%;max-height:70vh;border-radius:8px}
.modal-close{position:absolute;top:8px;right:12px;background:none;border:none;color:#fff;
      font-size:24px;cursor:pointer}
```

---

## 4. Backend Endpoint Contract (given)

| Method | Endpoint | Request | Response | UI action |
|--------|----------|---------|----------|-----------|
| POST | `/api/photos/auth` | — | `{ok:true}` or redirect | Begin OAuth → `location.href='/api/photos/auth'` |
| GET | `/api/photos/status` | — | `{connected:bool}` | Toggle status dot + panels |
| POST | `/api/photos/picker/create` | — | `{ok:true, picker_uri:"https://…", session_id:"…"}` | Open popup to `picker_uri` |
| GET | `/api/photos/picker/items` | — | `{items:[{id, name, thumbnail_url, mime, size}…]}` | Render preview grid |
| POST | `/api/photos/picker/import` | `{items:[ids]}` | `{results:[{id, name, ok, error?, size?}]}` | Render progress + totals |

> Notes for the implementer:
> - `picker/create` returns `picker_uri` (Google Picker URL). The frontend opens it in a **popup window** (`window.open`) so the SPA stays alive. Google Picker posts the selection back (via the `callback` configured server-side) and the backend stores the items; the frontend then calls `GET /picker/items`.
> - Poll `GET /picker/items` after the popup closes (poll every 1.5 s, up to ~20 s) until items appear, or rely on the popup writing a flag. Simpler robust approach: after `openPicker()`, start a poller that calls `/picker/items`; when it returns `items.length>0`, stop polling and render. Clear the session on import.
> - `picker/import` is expected to forward each selected media item to Telegram (`send_file` to `CHANNEL`/`me`, same pattern as `gdrive_copy`). The response shape mirrors `gdrive_copy`'s `{results:[…]}` so the progress renderer is reused almost verbatim.

---

## 5. JavaScript Flow

```js
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());

let selectedItems = [];   // [{id,name,thumbnail_url,mime,size}]
let sessionId = null;
let pickerWin = null;
let pollTimer = null;
let history = [];          // imported items (persisted in localStorage for offline view)

async function init(){
  const me = await api('/api/me');
  if(!me.logged_in){ location.href='/'; return; }
  await checkStatus();
  loadHistory();           // from localStorage cache
}

/* ---------- 1. CONNECTION STATUS ---------- */
async function checkStatus(){
  const d = await api('/api/photos/status');
  const dot=document.getElementById('statusDot');
  const txt=document.getElementById('statusText');
  const connBtn=document.getElementById('connectBtn');
  const discBtn=document.getElementById('disconnectBtn');
  const picker=document.getElementById('pickerSection');
  const empty=document.getElementById('connectEmpty');

  if(d.connected){
    dot.classList.add('on');
    txt.textContent='Terhubung ke Google Photos';
    connBtn.style.display='none';
    discBtn.style.display='inline-block';
    picker.style.display='block';
    empty.style.display='none';
  } else {
    dot.classList.remove('on');
    txt.textContent='Tidak terhubung';
    connBtn.style.display='inline-block';
    discBtn.style.display='none';
    picker.style.display='none';
    empty.style.display='block';
    clearSelection();
  }
}
function connectPhotos(){ location.href='/api/photos/auth'; }
async function disconnectPhotos(){
  if(!confirm('Putuskan koneksi Google Photos?')) return;
  await api('/api/photos/disconnect',{method:'POST'}).catch(()=>{});
  checkStatus();
}

/* ---------- 2. PICKER FLOW ---------- */
async function openPicker(){
  const r = await api('/api/photos/picker/create',{method:'POST'});
  if(!r.ok || !r.picker_uri){
    alert(r.error || 'Gagal membuat sesi picker');
    return;
  }
  sessionId = r.session_id || null;
  pickerWin = window.open(r.picker_uri, 'gphotos_picker',
                           'width=720,height=800,menubar=no,toolbar=no');
  // Poll server for selected items until popup closes or items arrive
  startItemPolling();
}
function startItemPolling(){
  clearInterval(pollTimer);
  let tries=0;
  pollTimer=setInterval(async ()=>{
    tries++;
    if(pickerWin && pickerWin.closed){ stopItemPolling(); return; }
    if(tries>30){ stopItemPolling(); return; }   // ~45s safety
    try{
      const d = await api('/api/photos/picker/items');
      if(d.items && d.items.length){
        stopItemPolling();
        selectedItems = d.items;
        renderPreview();
      }
    }catch(e){ /* ignore transient */ }
  }, 1500);
}
function stopItemPolling(){ clearInterval(pollTimer); pollTimer=null; }

/* ---------- 3. PREVIEW GRID ---------- */
function renderPreview(){
  const grid=document.getElementById('previewGrid');
  const title=document.getElementById('previewTitle');
  const importBtn=document.getElementById('importBtn');
  const clearBtn=document.getElementById('clearBtn');
  const cnt=document.getElementById('selCount');
  if(!selectedItems.length){
    grid.innerHTML=''; title.style.display='none';
    importBtn.disabled=true; clearBtn.disabled=true; cnt.textContent='0';
    return;
  }
  title.style.display='block';
  importBtn.disabled=false; clearBtn.disabled=false;
  cnt.textContent=selectedItems.length;
  grid.innerHTML=selectedItems.map((it,i)=>`
    <div class="file" onclick="previewPhoto('${encodeURIComponent(it.thumbnail_url||'')}','${it.name.replace(/'/g,"\\'")}')">
      <div class="thumb"><img src="${it.thumbnail_url}" loading="lazy" alt="${it.name}" onerror="this.parentNode.innerHTML='🖼️'"></div>
      <div class="info"><div class="name" title="${it.name}">${it.name}</div>
        <div class="meta"><span>${it.size?humanSize(it.size):''}</span></div></div>
      <button class="rm" onclick="event.stopPropagation();removeItem(${i})">✕</button>
    </div>`).join('');
}
function removeItem(i){
  selectedItems.splice(i,1);
  renderPreview();
}
function clearSelection(){
  selectedItems=[]; renderPreview();
}

/* ---------- 4. IMPORT + PROGRESS ---------- */
async function importSelected(){
  if(!selectedItems.length) return;
  const ids=selectedItems.map(it=>it.id);
  const panel=document.getElementById('progressPanel');
  const list=document.getElementById('progressList');
  const totals=document.getElementById('importTotals');
  panel.classList.add('show'); list.innerHTML=''; totals.innerHTML='';
  // seed progress rows
  list.innerHTML=ids.map(id=>{
    const it=selectedItems.find(x=>x.id===id);
    return `<div class="pitem" id="p_${id}"><span class="pname">${it?it.name:id}</span>
            <span class="pstatus run">⏳ …</span></div>`;
  }).join('');
  const r=await fetch('/api/photos/picker/import',
        {method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify({items:ids}),credentials:'same-origin'});
  const d=await r.json();
  let ok=0, fail=0;
  (d.results||[]).forEach(res=>{
    const id=res.id;
    const el=document.getElementById('p_'+id);
    if(!el) return;
    const st=el.querySelector('.pstatus');
    if(res.ok){ ok++; st.textContent='✅'; st.className='pstatus ok'; }
    else { fail++; st.textContent='❌ '+(res.error||'Gagal'); st.className='pstatus err'; }
    // add to history
    const src=selectedItems.find(x=>x.id===id);
    if(res.ok && src) pushHistory(src);
  });
  totals.innerHTML=`Selesai: <b class="ok">✅ ${ok}</b> · <b class="err">❌ ${fail}</b>`;
  // clear selection + refresh history view
  clearSelection();
  renderHistory();
}

/* ---------- 5. HISTORY ---------- */
function pushHistory(it){
  history.unshift({name:it.name, thumb:it.thumbnail_url, at:new Date().toISOString(), id:it.id});
  history=history.slice(0,200);
  localStorage.setItem('gphotos_history', JSON.stringify(history));
}
function loadHistory(){
  try{ history=JSON.parse(localStorage.getItem('gphotos_history')||'[]'); }catch(e){ history=[]; }
  renderHistory();
}
function renderHistory(){
  const grid=document.getElementById('historyGrid');
  const empty=document.getElementById('historyEmpty');
  const title=document.getElementById('historyTitle');
  if(!history.length){ grid.innerHTML=''; empty.style.display='block'; title.style.display='none'; return; }
  empty.style.display='none'; title.style.display='block';
  grid.innerHTML=history.map(h=>`
    <div class="file" onclick="previewPhoto('${encodeURIComponent(h.thumb||'')}','${h.name.replace(/'/g,"\\'")}')">
      <span class="badge">${fmtDate(h.at)}</span>
      <div class="thumb">${h.thumb?`<img src="${h.thumb}" loading="lazy" onerror="this.parentNode.innerHTML='🖼️'">`:'🖼️'}</div>
      <div class="info"><div class="name" title="${h.name}">${h.name}</div></div>
    </div>`).join('');
}

/* ---------- helpers ---------- */
function previewPhoto(url,name){
  if(!url) return;
  const box=document.getElementById('photoModal');
  document.getElementById('photoModalImg').src=decodeURIComponent(url);
  document.getElementById('photoModalName').textContent=name||'';
  box.classList.add('show');
}
function closePhotoModal(){ document.getElementById('photoModal').classList.remove('show'); }
function humanSize(n){ n=Number(n)||0; const u=['B','KB','MB','GB']; let i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(1)+' '+u[i]; }
function fmtDate(iso){ try{ const d=new Date(iso); return d.toLocaleDateString('id-ID',{day:'numeric',month:'short'}); }catch(e){ return ''; } }
async function doLogout(){ await api('/api/logout',{method:'POST'}); location.href='/'; }

document.addEventListener('keydown',e=>{ if(e.key==='Escape') closePhotoModal(); });
init();
```

### 5.1 Photo preview modal (add before `</body>`)

```html
<div class="modal" id="photoModal">
  <div class="modal-content">
    <button class="modal-close" onclick="closePhotoModal()">✕</button>
    <img id="photoModalImg" src="" alt="">
    <p id="photoModalName" style="text-align:center;color:var(--muted);font-size:13px;margin-top:8px"></p>
  </div>
</div>
```

---

## 6. State Machine (summary)

```
        ┌─────────────┐   GET /status  ┌──────────────┐
        │  init()     │ ─────────────► │ connected?    │
        └─────────────┘                └──────┬───────┘
                                              │
                       ┌──────────────────────┼───────────────────────┐
                       │ YES                  │ NO                    │
                       ▼                      ▼                       │
                pickerSection              connectEmpty              │
                (visible)                  + connectBtn              │
                       │                      │                       │
                [Buka Picker]           [Hubungkan] ──► /api/photos/auth (OAuth)
                       │                      │                       │  └─► redirect back, init()
                       ▼                      └───────────────────────┘
                picker/create ──► popup(picker_uri)
                       │
                 poll /picker/items (1.5s)
                       │ items arrive
                       ▼
                renderPreview()  ◄── selectedItems[]
                       │
                [Import ke Telegram]
                       ▼
                POST /picker/import {items}
                       │
                 progress rows + totals
                       ▼
                pushHistory() → renderHistory() ; clearSelection()
```

---

## 7. Mobile-Friendliness

- `viewport` meta already set. Grids use `auto-fill, minmax(150px,1fr)` so they collapse to 1–2 columns on phones.
- `.toolbar`/`.status-bar` use `flex-wrap:wrap` so buttons stack.
- Sticky `.topbar` keeps navigation reachable while scrolling the history grid.
- Popup dimensions `720x800` fall back gracefully; on small screens the browser opens Picker as a new tab (acceptable). Provide a note: if popup blocked, the user is redirected to `picker_uri` in the same tab and returns via the OAuth/callback redirect.
- Touch targets ≥ 26px (`.rm`, `.btn` padding ≥10px).

---

## 8. Performance

- Inline CSS/JS, zero external requests except Google Picker `picker_uri` (opens in its own window) and thumbnail images (`loading="lazy"`, `object-fit:cover`, `onerror` fallback to 🖼️ emoji so broken thumbnails never break layout).
- No frameworks, no web-font fetch. Uses system font stack.
- History persisted in `localStorage` → instant render on revisit, no extra API call.
- Polling capped at 30 ticks / 45 s; cleared as soon as items arrive or popup closes.

---

## 9. Implementation Notes for the Backend Dev

1. Add a Flask route `/photos` returning `PHOTOS_HTML` (mirror `gdrive_page()`). Guard with `login_required` or the `if not session.get("user_id")` → `redirect('/')` pattern.
2. Add the 5 endpoints from §4. `picker/create` must create a Google Picker session and return `picker_uri` + `session_id`. `picker/items` returns the items the Picker posted back (store server-side keyed by `session_id` or user session). `picker/import` forwards each item to Telegram and returns `{results:[…]}`.
3. If a Google Photos **Library** download is needed (Picker gives limited-resolution media), implement server-side fetch of the full-resolution media URL before `send_file`. Document this in `.help`.
4. Add `PHOTOS_CLIENT_ID` / `PHOTOS_CLIENT_SECRET` (or `PHOTOS_CREDENTIALS_PATH`) handling mirroring `web_drive.py`'s `_client_config()`.
5. Optional: add a `google_photos_history` DB table so history survives cache clears; the frontend can then `GET /api/photos/history` and merge with localStorage. Keep localStorage as the fast path.

---

## 10. Acceptance Checklist

- [ ] `/photos` reachable, dark purple, no glassmorphism, matches Drive/GDrive look.
- [ ] Status dot green/red reflects `GET /status`.
- [ ] "Hubungkan" triggers OAuth; on return the picker controls appear.
- [ ] "Buka Google Photos Picker" opens popup, selections populate the preview grid.
- [ ] Preview grid shows thumbnails; tap opens modal; ✕ removes one.
- [ ] "Import ke Telegram" shows per-item progress with ✅/❌, then totals.
- [ ] History grid lists imported photos with date badge; persists across reloads.
- [ ] "← Kembali ke Drive" returns to `/drive`.
- [ ] Layout usable on a 375px-wide phone.
```
