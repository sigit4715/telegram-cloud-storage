from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
for needle in [
    '.stat.active', '.type-item.active', 'data-view="files"',
    'data-kind="', 'function markActiveView',
    'function setViewMode', 'loadCurrentView()',
    "per_page='+perPage", 'renderPagination(d.page,d.pages)'
]:
    assert needle in src, f'missing {needle}'
print('PASS: active controls and paginated views wired')
