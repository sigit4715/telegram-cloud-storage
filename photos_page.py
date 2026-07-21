# GOOGLE PHOTOS PAGE - injected into web_drive.py

PHOTOS_PAGE_HTML = '''<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Photos - Cloud Storage</title>
<style>
:root{--bg:#0d0221;--surface:#1a1a2e;--card:#16213e;--accent:#a855f7;--accent2:#7c3aed;
--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--text:#e0e0e0;--muted:#888;--border:rgba(255,255,255,0.08)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:var(--surface);padding:12px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
.topbar a{color:var(--accent);text-decoration:none;font-size:14px}
.topbar h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.container{max-width:960px;margin:0 auto;padding:20px}
.status-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}
.status-row{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--red);transition:.3s}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.status-text{font-size:14px;color:var(--muted)}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:.2s;display:inline-flex;align-items:center;gap:8px}
.btn:hover{transform:translateY(-1px)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-purple{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.btn-green{background:linear-gradient(135deg,#059669,#34d399);color:#fff}
.btn-blue{background:linear-gradient(135deg,var(--blue),#60a5fa);color:#fff}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--muted)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;margin-top:12px}
.grid img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;background:var(--card)}
.picker-section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px;display:none}
.import-section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px;display:none}
.progress-bar{height:4px;background:var(--card);border-radius:4px;overflow:hidden;margin:10px 0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--green));transition:width .3s;border-radius:4px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;font-size:14px;font-weight:600;z-index:999;animation:slideUp .3s}
.toast.ok{background:var(--green);color:#fff}
.toast.err{background:var(--red);color:#fff}
@keyframes slideUp{from{transform:translateX(-50%) translateY(20px);opacity:0}to{transform:translateX(-50%) translateY(0);opacity:1}}
@media(max-width:600px){.topbar{padding:10px 14px}.container{padding:14px}.topbar h1{font-size:16px}}
</style></head><body>
<div class="topbar"><a href="/files">&#8592; Files</a><h1>Google Photos</h1></div>
<div class="container">
  <div class="status-card">
    <div class="status-row">
      <div class="dot" id="pDot"></div>
      <span class="status-text" id="pText">Memeriksa...</span>
    </div>
    <button class="btn btn-purple" id="pConnBtn" style="display:none" onclick="connectPhotos()">Hubungkan Google Photos</button>
    <button class="btn btn-blue" id="pPickBtn" style="display:none" onclick="openPicker()">Pilih Foto dari Google Photos</button>
  </div>

  <div class="picker-section" id="pickSec">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px">
      <span style="font-size:14px;color:var(--accent);font-weight:600" id="pickCount"></span>
      <button class="btn btn-green" id="importBtn" onclick="importPhotos()">Import ke Telegram</button>
    </div>
    <div class="grid" id="pickGrid"></div>
  </div>

  <div class="import-section" id="importSec">
    <div style="font-size:14px;font-weight:600;margin-bottom:8px">Mengimport foto...</div>
    <div class="progress-bar"><div class="progress-fill" id="importFill" style="width:0%"></div></div>
    <div style="font-size:13px;color:var(--muted)" id="importStatus"></div>
  </div>

  <div style="padding:16px;background:var(--surface);border:1px solid var(--border);border-radius:12px;font-size:13px;color:var(--muted)">
    <strong style="color:var(--accent)">Cara kerja:</strong><br>
    1. Klik "Hubungkan Google Photos" (kalau belum connect)<br>
    2. Klik "Pilih Foto" - pilih foto di Google Photos<br>
    3. Klik "Done" di picker<br>
    4. Klik "Import ke Telegram"<br>
    <br><em>Foto akan masuk ke folder "Google Photos" di Telegram.</em>
  </div>
</div>

<script>
const api=(u,o)=>fetch(u,{credentials:'same-origin',...o}).then(r=>r.json());
let pollTimer=null;
let userEmail='';

async function init(){
  const me=await api('/api/me');
  if(!me.logged_in){location.href='/';return;}
  userEmail=me.email||'';
  checkStatus();
}

async function checkStatus(){
  const d=await api('/api/photos/status');
  const dot=document.getElementById('pDot');
  const txt=document.getElementById('pText');
  const conn=document.getElementById('pConnBtn');
  const pick=document.getElementById('pPickBtn');
  if(d.connected){
    dot.classList.add('on');
    txt.textContent='Terhubung ke Google Photos'+(userEmail?' ('+userEmail+')':'');
    conn.style.display='none';
    pick.style.display='inline-flex';
  }else{
    dot.classList.remove('on');
    txt.textContent='Tidak terhubung ke Google Photos';
    conn.style.display='inline-flex';
    pick.style.display='none';
  }
}

function connectPhotos(){location.href='/api/photos/auth';}

async function openPicker(){
  const r=await api('/api/photos/picker/create',{method:'POST'});
  if(r.error){toast(r.error,true);return;}
  window.open(r.picker_uri,'_blank','width=800,height=600');
  toast('Picker dibuka! Pilih foto, lalu klik Done.',false);
  setTimeout(pollItems,3000);
}

async function pollItems(){
  const d=await api('/api/photos/picker/items');
  if(d.error||!d.items||!d.items.length){
    pollTimer=setTimeout(pollItems,2000);
    return;
  }
  clearTimeout(pollTimer);
  const sec=document.getElementById('pickSec');
  const grid=document.getElementById('pickGrid');
  const cnt=document.getElementById('pickCount');
  sec.style.display='block';
  cnt.textContent=d.count+' foto/video dipilih';
  grid.innerHTML=d.items.slice(0,30).map(it=>
    '<img src="'+it.baseUrl+'=w160-h160-c" loading="lazy">'
  ).join('')+(d.items.length>30?'<div style="display:flex;align-items:center;justify-content:center;aspect-ratio:1;border-radius:8px;background:var(--card);font-size:12px;color:var(--muted)">+'+(d.items.length-30)+'</div>':'');
}

async function importPhotos(){
  if(!confirm('Import foto dari Google Photos ke Telegram?'))return;
  const btn=document.getElementById('importBtn');
  const sec=document.getElementById('importSec');
  const fill=document.getElementById('importFill');
  const st=document.getElementById('importStatus');
  btn.disabled=true;
  sec.style.display='block';
  fill.style.width='30%';
  st.textContent='Mengunduh dan mengupload...';

  const d=await api('/api/photos/picker/items');
  if(!d.items||!d.items.length){toast('Tidak ada foto',true);btn.disabled=false;return;}

  const ids=d.items.map(i=>i.id);
  fill.style.width='60%';
  const r=await api('/api/photos/picker/import',{method:'POST',body:JSON.stringify({item_ids:ids})});

  fill.style.width='100%';
  btn.disabled=false;
  if(r.error){st.textContent='Error: '+r.error;st.style.color='var(--red)';return;}
  st.innerHTML='Berhasil import <strong>'+r.imported+'</strong> foto!'+(r.errors.length?' ('+r.errors.length+' error)':'');
  st.style.color='var(--green)';
  toast('Import '+r.imported+' foto berhasil!',false);
  document.getElementById('pickSec').style.display='none';
}

function toast(msg,err){
  const t=document.createElement('div');
  t.className='toast '+(err?'err':'ok');
  t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(()=>t.remove(),4000);
}

init();
</script>
</body></html>'''
