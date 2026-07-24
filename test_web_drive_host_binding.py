import sys
import unittest


class WebDriveHostBindingTests(unittest.TestCase):
    def test_web_import_registers_owner_scoped_settings_once(self):
        import web
        import web_drive

        rules = [r.rule for r in web.app.url_map.iter_rules()]
        self.assertEqual(rules.count('/api/settings/telegram'), 2)  # GET + POST
        self.assertEqual(rules.count('/api/settings/telegram/test'), 1)
        self.assertTrue(web_drive._HAS_HOST)
        self.assertIs(sys.modules.get('web'), web)
        self.assertIs(web_drive.db_query, web.db_query)
        self.assertIs(web_drive.get_telegram_client, web.get_telegram_client)
        self.assertTrue(web_drive.DB_PATH.endswith('storage.db'))


if __name__ == '__main__':
    unittest.main()
