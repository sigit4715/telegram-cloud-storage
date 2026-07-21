from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
for marker in ['gd-btn gd-btn-connect','gd-btn gd-btn-disconnect','gd-btn gd-btn-close','gd-btn gd-btn-sync','gd-btn gd-btn-copy','.gd-btn{','.gd-btn-connect{','.gd-btn-disconnect{','.gd-btn-close{','.gd-btn-sync{','.gd-btn-copy{']:
    assert marker in src, marker
print('PASS: embedded Google Drive controls have dedicated polished styles')
