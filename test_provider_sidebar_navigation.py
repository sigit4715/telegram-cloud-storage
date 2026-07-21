from pathlib import Path
src=Path(__file__).with_name('web.py').read_text()
for marker in [
    'data-nav="gdrive"', 'data-nav="gphotos"',
    'onclick="openGDrivePanel(this)"', 'onclick="openGPhotosPanel(this)"',
    'id="gphotosPanel"', 'id="gphotosFrame"',
    'function openGPhotosPanel(', 'function closeProviderPanels(',
    "function openNav(section,el){\n  closeProviderPanels();"
]:
    assert marker in src, marker
print('PASS: Google Drive and Photos are navigable from the persistent sidebar')
