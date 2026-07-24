from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
assert 'def _page_params()' in src
assert 'request.args.get("q", "").strip()' in src
assert '"pages": pages' in src
assert "currentView='dashboard'" in src
assert 'function loadCurrentView()' in src
assert 'renderPagination(d.page,d.pages)' in src
assert "per_page='+perPage" in src
print('PASS: global views support search and pagination')
