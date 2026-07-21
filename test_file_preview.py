from pathlib import Path
s=Path(__file__).with_name('web.py').read_text()
markers=[
 '@app.route("/api/stream/<int:fid>")',
 'Content-Range',
 'Accept-Ranges',
 '@app.route("/api/office-preview/<int:fid>")',
 'soffice',
 'function openFilePreview(',
 '/api/office-preview/',
 '/api/stream/',
 'playsinline preload="metadata"',
 'class="preview-text',
 'closeFilePreview()',
 'is_office',
 'is_text',
 'preview-btn',
]
missing=[x for x in markers if x not in s]
assert not missing, 'missing: '+repr(missing)
print('PASS: image video audio PDF text and Office preview markers present')
