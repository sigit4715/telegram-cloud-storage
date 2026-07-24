import unittest
from unittest import mock

import web


class TelegramTargetNormalizationTests(unittest.TestCase):
    def test_bare_channel_id_is_normalized_to_telegram_peer_id(self):
        with mock.patch.object(web, "require_telegram_config", return_value={"channel_id": 3808532093}):
            self.assertEqual(web.telegram_target("bowor4751@gmail.com"), -1003808532093)

    def test_negative_peer_id_is_preserved(self):
        with mock.patch.object(web, "require_telegram_config", return_value={"channel_id": -1003808532093}):
            self.assertEqual(web.telegram_target("bowor4751@gmail.com"), -1003808532093)


if __name__ == "__main__":
    unittest.main()
