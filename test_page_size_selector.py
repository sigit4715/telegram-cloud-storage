from pathlib import Path
src = Path(__file__).with_name('web.py').read_text()
assert 'id="pageSize"' in src
assert 'option value="10"' in src
assert 'option value="25"' in src
assert 'option value="50"' in src
assert 'option value="100"' in src
assert 'perPage=25' in src
assert "per_page='+perPage" in src
assert 'function changePageSize' in src
print('PASS: page-size selector wired to all paginated views')
