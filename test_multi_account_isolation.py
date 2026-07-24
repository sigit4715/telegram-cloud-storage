import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

# Importing web must not start Telethon; the app starts it only under __main__.
# web.py reads config.env directly. Temporarily substitute it during import;
# restore the production/development config immediately afterward.
from pathlib import Path
_config_path = Path(__file__).with_name("config.env")
_config_backup = _config_path.read_bytes()
_config_path.write_text("API_ID=0\nCHANNEL=0\n")
try:
    import web
finally:
    _config_path.write_bytes(_config_backup)


class MultiAccountIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "storage.db")
        self.old_db = web.DB_PATH
        web.DB_PATH = self.db_path
        web.app.config.update(TESTING=True, SECRET_KEY="test-isolation")
        web.init_db()
        self.client = web.app.test_client()
        self.owner_a = "bowor4751@gmail.com"
        self.owner_b = "sigitputraprabowo01@gmail.com"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO folders(name,parent_id,owner_email) VALUES('Dokumen',0,?)", (self.owner_a,))
            conn.execute("INSERT INTO folders(name,parent_id,owner_email) VALUES('Dokumen',0,?)", (self.owner_b,))
            a_folder = conn.execute("SELECT id FROM folders WHERE name='Dokumen' AND owner_email=?", (self.owner_a,)).fetchone()[0]
            b_folder = conn.execute("SELECT id FROM folders WHERE name='Dokumen' AND owner_email=?", (self.owner_b,)).fetchone()[0]
            conn.execute("INSERT INTO files(file_name,msg_id,size,mime,folder_id,owner_email) VALUES('rahasia-a.pdf',101,100,'application/pdf',?,?)", (a_folder,self.owner_a))
            conn.execute("INSERT INTO files(file_name,msg_id,size,mime,folder_id,owner_email) VALUES('rahasia-b.pdf',202,200,'application/pdf',?,?)", (b_folder,self.owner_b))
            conn.commit()
        self.a_file = self._scalar("SELECT id FROM files WHERE owner_email=?", (self.owner_a,))
        self.b_file = self._scalar("SELECT id FROM files WHERE owner_email=?", (self.owner_b,))
        self.a_folder = self._scalar("SELECT id FROM folders WHERE owner_email=?", (self.owner_a,))
        self.b_folder = self._scalar("SELECT id FROM folders WHERE owner_email=?", (self.owner_b,))

    def tearDown(self):
        web.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _scalar(self, sql, params=()):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def login(self, email):
        with self.client.session_transaction() as sess:
            sess["user_id"] = email
            sess["google_email"] = email

    def test_lists_stats_and_search_are_scoped_to_current_owner(self):
        self.login(self.owner_b)
        root = self.client.get("/api/folders?parent_id=0").get_json()
        stats = self.client.get("/api/stats").get_json()
        files = self.client.get(f"/api/files?folder_id={self.b_folder}&q=rahasia").get_json()
        self.assertEqual([f["name"] for f in root["folders"]], ["Dokumen"])
        self.assertEqual(stats["total_files"], 1)
        self.assertEqual([f["name"] for f in files["files"]], ["rahasia-b.pdf"])

    def test_cross_owner_ids_are_not_readable_or_mutable(self):
        self.login(self.owner_b)
        self.assertEqual(self.client.get(f"/api/files?folder_id={self.a_folder}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/download/{self.a_file}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/delete/{self.a_file}").status_code, 404)
        self.assertEqual(self.client.post("/api/move", json={"file_ids":[self.b_file], "folder_id":self.a_folder}).status_code, 404)
        self.assertIsNone(self._scalar("SELECT deleted_at FROM files WHERE id=?", (self.a_file,)))

    def test_new_google_account_has_empty_storage(self):
        self.login("brandnew@gmail.com")
        self.assertEqual(self.client.get("/api/folders?parent_id=0").get_json()["folders"], [])
        self.assertEqual(self.client.get("/api/files?folder_id=0").get_json()["total"], 0)
        self.assertEqual(self.client.get("/api/stats").get_json()["total_files"], 0)

    def test_upload_records_owner_email(self):
        self.login(self.owner_b)
        fake_message = type("Message", (), {"id": 303})()
        fake_client = type("Client", (), {"send_file": lambda *args, **kwargs: object()})()
        with patch.object(web, "get_telegram_client", return_value=fake_client), patch.object(web, "telegram_target", return_value=-100303), patch.object(web, "run_async", return_value=fake_message):
            resp = self.client.post("/api/upload", data={"files": (tempfile.SpooledTemporaryFile(), "baru.txt"), "folder_id": "0"}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["results"][0]["ok"])
        self.assertEqual(self._scalar("SELECT owner_email FROM files WHERE msg_id=303"), self.owner_b)


if __name__ == "__main__":
    unittest.main()
