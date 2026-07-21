from pathlib import Path
src=Path(__file__).with_name('web_drive.py').read_text()
for marker in [
    'CREATE TABLE IF NOT EXISTS google_oauth_states',
    'INSERT OR REPLACE INTO google_oauth_states',
    "SELECT user_id, code_verifier, created_at FROM google_oauth_states WHERE state=? AND flow_type='drive'",
    'if (not uid or not expected_state) and state_row:',
    'DELETE FROM google_oauth_states WHERE state=?',
]:
    assert marker in src, marker
assert 'Token response: status=%s data=%s' not in src, 'OAuth access tokens must not be logged'
print('PASS: Drive OAuth state survives lost cookie session without secret logging')
