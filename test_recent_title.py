from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
block=src.split('function loadNavPage(){',1)[1].split('\n}',1)[0]
assert "recent:'File Terbaru'" in block
assert "labels[currentView]" in block
assert "document.getElementById('folderTitle').textContent" not in block
print('PASS: recent title is stable during async folder loading')
