"""Regression smoke for systemd entrypoint: python web.py runs as __main__.

web_drive imports `web`; this must resolve to the running __main__ module, not
execute a second copy of web.py with a second Flask app/config/client cache.
"""
import runpy
import sys
import unittest
from unittest import mock


class WebMainEntrypointAliasTests(unittest.TestCase):
    def test_running_web_as_main_aliases_web_module_before_web_drive_import(self):
        for name in ["web", "web_drive"]:
            sys.modules.pop(name, None)

        with mock.patch("flask.Flask.run") as run_mock:
            ns = runpy.run_path("web.py", run_name="__main__")

        main_mod = sys.modules.get("web")
        web_drive = sys.modules.get("web_drive")
        self.assertIsNotNone(main_mod)
        self.assertIs(web_drive.db_query, ns["db_query"])
        self.assertIs(web_drive.get_telegram_client, ns["get_telegram_client"])
        self.assertIs(web_drive.run_async, ns["run_async"])
        rules = [r.rule for r in ns["app"].url_map.iter_rules()]
        self.assertEqual(rules.count("/api/settings/telegram"), 2)
        self.assertEqual(rules.count("/api/settings/telegram/test"), 1)
        run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
