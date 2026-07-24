from pathlib import Path

src = Path(__file__).with_name("web.py").read_text()
for handler in ["openAllFiles", "openStorageFiles", "openFolderOverview", "openFileType"]:
    assert handler in src, f"missing click handler: {handler}"
assert 'class="stat" role="button"' in src
print("PASS: dashboard stat cards are interactive")
