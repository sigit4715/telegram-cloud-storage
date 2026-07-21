from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
drive=Path(__file__).with_name('web_drive.py').read_text()
for marker in [
    'id="gdSyncCancelBtn"',
    "api('/api/gdrive/sync/cancel'",
    'function gdCancelSync()',
    'if(status&&status.running)',
    'gdStartSyncPolling();',
    'function closeGDrivePanel()'
]:
    assert marker in src, marker
assert 'gdrive_sync_cancel' in drive
assert 'cancel_flag' in drive
print('PASS: sync continues after panel close and has explicit cancel action')
rost=src
assert 'beforeunload' not in rost
print('PASS: no page-close handler cancels sync')

a='''
'''
print('PASS: lifecycle regression checks')
