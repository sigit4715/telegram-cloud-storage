"""Regression test for auto-redirecting logged in users from / to /drive."""

import unittest
from unittest import mock
import importlib

import web


class LoginRedirectTests(unittest.TestCase):
    def test_guest_sees_login_page(self):
        client = web.app.test_client()
        resp = client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Login', resp.data)

    def test_logged_in_user_redirects_to_drive(self):
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = '5337119189' # Admin ID or allowed user
        resp = client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith('/drive'))


if __name__ == "__main__":
    unittest.main()
