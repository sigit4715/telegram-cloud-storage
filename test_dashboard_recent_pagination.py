from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
block=src.split('function loadRecentDashboard(){',1)[1].split('\n}',1)[0]
assert "api('/api/recent?limit=5000')" not in block
assert 'loadNavPage();' in block
assert "currentView='recent'" in block
print('PASS: dashboard recent uses normal paginated/searchable loader')
