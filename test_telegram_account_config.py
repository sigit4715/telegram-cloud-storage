import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import telegram_accounts as ta


class TelegramAccountConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "storage.db")
        self.key = "unit-test-key-never-used-in-production"
        ta.init_telegram_accounts(self.db)

    def tearDown(self):
        ta.close_all_clients()
        self.tmp.cleanup()

    def test_accounts_are_isolated_and_secrets_are_not_returned(self):
        ta.save_account_config(self.db, self.key, "alice@example.com", {
            "api_id": "12345", "api_hash": "alice-hash", "bot_token": "111:alice", "channel_id": "-100111"
        })
        ta.save_account_config(self.db, self.key, "bob@example.com", {
            "api_id": "67890", "api_hash": "bob-hash", "bot_token": "222:bob", "channel_id": "-100222"
        })

        alice = ta.get_account_config(self.db, self.key, "alice@example.com")
        bob = ta.get_account_config(self.db, self.key, "bob@example.com")
        self.assertEqual(alice["bot_token"], "111:alice")
        self.assertEqual(bob["bot_token"], "222:bob")
        self.assertNotEqual(alice["channel_id"], bob["channel_id"])

        public = ta.get_public_config(self.db, "alice@example.com")
        self.assertTrue(public["configured"])
        self.assertEqual(public["api_id"], "12345")
        self.assertEqual(public["channel_id"], "-100111")
        self.assertNotIn("api_hash", public)
        self.assertNotIn("bot_token", public)
        self.assertTrue(public["api_hash_set"])
        self.assertTrue(public["bot_token_set"])

    def test_blank_secret_keeps_existing_value(self):
        ta.save_account_config(self.db, self.key, "alice@example.com", {
            "api_id": "12345", "api_hash": "old-hash", "bot_token": "111:old", "channel_id": "-100111"
        })
        ta.save_account_config(self.db, self.key, "alice@example.com", {
            "api_id": "12345", "api_hash": "", "bot_token": "", "channel_id": "-100111"
        })
        cfg = ta.get_account_config(self.db, self.key, "alice@example.com")
        self.assertEqual(cfg["api_hash"], "old-hash")
        self.assertEqual(cfg["bot_token"], "111:old")

    def test_legacy_config_migrates_only_to_bowor(self):
        ta.migrate_legacy_config(self.db, self.key, "bowor4751@gmail.com", {
            "API_ID": "999", "API_HASH": "legacy-hash", "BOT_TOKEN": "999:legacy", "CHANNEL": "-100999"
        })
        self.assertTrue(ta.get_public_config(self.db, "bowor4751@gmail.com")["configured"])
        self.assertFalse(ta.get_public_config(self.db, "new@example.com")["configured"])

    def test_settings_routes_use_logged_in_google_owner(self):
        import web_drive
        from flask import Flask
        web_drive.DB_PATH = self.db
        web_drive.TELEGRAM_CONFIG_KEY = self.key
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(web_drive.drive_ext)

        alice = app.test_client()
        with alice.session_transaction() as sess:
            sess["google_email"] = "alice@example.com"
            sess["user_id"] = "alice@example.com"
        with mock.patch.object(web_drive, "TELEGRAM_CONFIG_KEY", self.key):
            response = alice.post("/api/settings/telegram", json={
                "api_id": "12345", "api_hash": "a" * 32,
                "bot_token": "123:alice-token", "channel_id": "-100111"
            })
            self.assertEqual(response.status_code, 200)

            bob = app.test_client()
            with bob.session_transaction() as sess:
                sess["google_email"] = "bob@example.com"
                sess["user_id"] = "bob@example.com"
            self.assertFalse(bob.get("/api/settings/telegram").get_json()["configured"])
            self.assertEqual(int(alice.get("/api/settings/telegram").get_json()["api_id"]), 12345)

    def test_migration_is_idempotent(self):
        ta.save_account_config(self.db, self.key, "bowor4751@gmail.com", {
            "api_id": "1000", "api_hash": "new-hash", "bot_token": "1000:new", "channel_id": "-1001000"
        })
        ta.migrate_legacy_config(self.db, self.key, "bowor4751@gmail.com", {
            "API_ID": "999", "API_HASH": "legacy-hash", "BOT_TOKEN": "999:legacy", "CHANNEL": "-100999"
        })
        self.assertEqual(ta.get_account_config(self.db, self.key, "bowor4751@gmail.com")["api_id"], 1000)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            ta.save_account_config(self.db, self.key, "alice@example.com", {
                "api_id": "abc", "api_hash": "h", "bot_token": "t", "channel_id": "-1001"
            })
        with self.assertRaises(ValueError):
            ta.save_account_config(self.db, self.key, "alice@example.com", {
                "api_id": "1", "api_hash": "h", "bot_token": "t", "channel_id": "not-a-channel"
            })


if __name__ == "__main__":
    unittest.main()
