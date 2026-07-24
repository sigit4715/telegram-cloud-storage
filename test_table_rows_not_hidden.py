from pathlib import Path
src = Path(__file__).with_name('web.py').read_text()
assert '.files-table tbody tr:nth-child(n+6){display:none}' not in src
assert '.pagination{display:none}' not in src
print('PASS: desktop stylesheet does not hide file rows or pagination')
