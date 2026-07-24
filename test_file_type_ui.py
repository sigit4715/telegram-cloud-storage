from pathlib import Path

src = Path(__file__).with_name("web.py").read_text()

assert '@app.route("/api/files/type/<kind>")' in src
assert "onclick=\"openFileType" in src
assert "function openFileType(kind)" in src
assert "onclick=\"showAllFileTypes(event)\"" in src
assert "onclick=\"showAllRecent(event)\"" in src
print("PASS: file-type and see-all controls are wired")
