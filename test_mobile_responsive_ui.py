from pathlib import Path
src = Path(__file__).with_name('web.py').read_text()
for marker in [
    '@media(max-width:600px)',
    '.mobile-menu-btn',
    '.mobile-sidebar-open .sidebar',
    '.mobile-bottom-nav',
    '.gd-toolbar',
    '.files-table th:nth-child(2)',
]:
    assert marker in src, marker
print('PASS: mobile responsive layout markers present')
