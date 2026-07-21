from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
checks=[
    'id="gdSyncProgressBar"',
    'id="gdSyncProgressText"',
    "api('/api/gdrive/sync/status?t='+Date.now())",
    'setInterval(gdPollSync,1000)',
    'function gdPollSync()',
]
for marker in checks:
    assert marker in src, marker
print('PASS: Google Drive sync exposes a live progress bar and polling')

# Ensure the backend status contract used by the UI is present.
drive=Path(__file__).with_name('web_drive.py').read_text()
for marker in ['@drive_ext.route("/api/gdrive/sync/status")', '"percent": pct', '"current": _sync_state.get("current", "")']:
    assert marker in drive, marker
print('PASS: sync status endpoint exposes percent/current')
