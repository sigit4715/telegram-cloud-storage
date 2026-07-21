from pathlib import Path
web = Path(__file__).with_name('web.py').read_text()
drive = Path(__file__).with_name('web_drive.py').read_text()
for marker in [
    'id="gdSyncSuccessCount"',
    'id="gdSyncErrorCount"',
    'id="gdSyncFailures"',
    'id="gdRetryFailedBtn"',
    'function gdRetryFailed()',
    'Number(d.imported)||0',
    'Number(d.errors_count)||0',
    'd.storage_total!==undefined',
    'loadStats();',
]:
    assert marker in web, marker
for marker in [
    'CREATE TABLE IF NOT EXISTS sync_errors',
    '"imported": _sync_state["imported"]',
    'DELETE FROM sync_errors WHERE drive_file_id=?',
]:
    assert marker in drive, marker
print('PASS: sync exposes success/failure results and retry UI')
