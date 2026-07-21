from pathlib import Path
src = Path(__file__).with_name('web.py').read_text()
assert '@app.route("/api/trash/<int:fid>/permanent", methods=["DELETE"])' in src
assert 'SELECT id, msg_id FROM files WHERE id=? AND deleted_at IS NOT NULL' in src
assert 'telethon_client.delete_messages(CHANNEL, [row[0]["msg_id"]])' in src
assert 'data-action="purgeFile"' in src
assert 'data-action="restoreFile"' in src
assert 'function purgeFile(id,name)' in src
assert 'function restoreFile(id)' in src
print('PASS: trash supports explicit permanent Telegram deletion and restore')
