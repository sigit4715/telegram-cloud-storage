from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
for marker in [
    'id="gdrivePanel"',
    'function openGDrivePanel()',
    'function closeGDrivePanel()',
    'function gdLoadFiles(folderId)',
    "api('/api/gdrive/status')",
    "api('/api/gdrive/files?folder_id='+encodeURIComponent(folderId))",
    "api('/api/gdrive/copy'",
    "api('/api/gdrive/sync?t='+Date.now())",
    "onclick=\"openGDrivePanel()\"",
    'gdrive-mode'
]:
    assert marker in src, marker
print('PASS: /drive embeds Google Drive panel without leaving page')
