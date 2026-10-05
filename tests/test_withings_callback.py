import os
import tempfile
import unittest
from pathlib import Path

from health_workbook.withings_callback import FAILURE, READY, SUCCESS, decide


class WithingsCallbackTests(unittest.TestCase):
    def test_ready_page_has_no_exchange(self):
        called = []

        def transport(url, fields):
            called.append(url)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = Path(directory) / "withings.env"
            status, page = decide({}, state, env, transport)
        self.assertEqual(status, 200)
        self.assertEqual(page, READY)
        self.assertEqual(called, [])

    def test_bad_state_does_not_exchange_or_write(self):
        called = []

        def transport(url, fields):
            called.append(fields)
            return {"status": 0, "body": {"access_token": "secret-access", "refresh_token": "secret-refresh"}}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = Path(directory) / "withings.env"
            state.write_text("expected\n", encoding="utf-8")
            env.write_text("WITHINGS_CLIENT_ID=id\nWITHINGS_CLIENT_SECRET=sec\n", encoding="utf-8")
            status, page = decide({"code": "abc", "state": "wrong"}, state, env, transport)
            text = env.read_text(encoding="utf-8")
        self.assertEqual((status, page), (400, FAILURE))
        self.assertEqual(called, [])
        self.assertNotIn("secret-access", text)
        self.assertNotIn("secret-refresh", page)

    def test_exchange_writes_refresh_token_and_hides_it(self):
        def transport(url, fields):
            self.assertEqual(fields["grant_type"], "authorization_code")
            self.assertEqual(fields["redirect_uri"], "https://withings.syzygylab.net/callback")
            return {"status": 0, "body": {"access_token": "secret-access", "refresh_token": "secret-refresh"}}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = Path(directory) / "withings.env"
            state.write_text("expected\n", encoding="utf-8")
            env.write_text("WITHINGS_CLIENT_ID=id\nWITHINGS_CLIENT_SECRET=sec\n", encoding="utf-8")
            status, page = decide({"code": "abc", "state": "expected"}, state, env, transport)
            text = env.read_text(encoding="utf-8")
            mode = os.stat(env).st_mode & 0o777
        self.assertEqual((status, page), (200, SUCCESS))
        self.assertNotIn("secret-access", page)
        self.assertNotIn("secret-refresh", page)
        self.assertIn("WITHINGS_REFRESH_TOKEN=secret-refresh\n", text)
        if os.name == "posix":
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
