"""Regression: Telegram Settings is a standalone page at /settings/telegram."""

import unittest
from unittest import mock
import importlib

import web_drive


class TelegramSettingsPageTests(unittest.TestCase):
    def test_route_exists_and_returns_200(self):
        mod = importlib.import_module('web')
        client = mod.app.test_client()
        with client.session_transaction() as sess:
            sess['google_email'] = 'test@example.com'
            sess['user_id'] = '123456'
        resp = client.get('/settings/telegram')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Pengaturan Telegram', resp.data)

    def test_page_loads_own_form_not_embedded(self):
        mod = importlib.import_module('web')
        client = mod.app.test_client()
        with client.session_transaction() as sess:
            sess['google_email'] = 'test@example.com'
            sess['user_id'] = '123456'
        resp = client.get('/settings/telegram')
        html = resp.data.decode('utf-8')
        self.assertIn('id="api_id"', html)
        self.assertIn('id="channel_id"', html)
        self.assertIn('/settings/telegram', html)

    def test_sidebar_links_to_settings_telegram(self):
        mod = importlib.import_module('web')
        html = mod.DRIVE_HTML
        self.assertIn('/settings/telegram', html)
        self.assertIn('Pengaturan Telegram', html)


if __name__ == "__main__":
    unittest.main()
