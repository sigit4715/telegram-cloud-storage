from pathlib import Path
src = Path(__file__).with_name('web.py').read_text()
assert 'limit = max(1, min(5000' in src
assert "currentView='recent';" in src
assert "var url='/api/navigation/'+currentView+'?page='+currentPage+'&per_page='+perPage;" in src
print('PASS: dashboard recent list is not capped at five')
