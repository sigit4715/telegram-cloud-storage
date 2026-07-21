from pathlib import Path
src=Path(__file__).with_name('web_drive.py').read_text()
for marker in [
    'class="app-sidebar"', 'class="drive-hero"', 'Terhubung ke Google Drive',
    'class="folder-list"', 'Copy ke Telegram (<span id="selCount">0</span>)',
    'href="/drive"', 'href="/photos"', 'id="searchInput"',
]:
    assert marker in src, marker
print('PASS: gdrive reference layout markers present')
