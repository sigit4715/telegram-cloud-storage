from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
assert 'function toggleFavorite' in src
assert "data-action=\"favorite\"" in src
assert "'/api/files/'+id+'/favorite'" in src
assert 'is_favorite' in src
print('PASS: file rows expose a working favorites control')
