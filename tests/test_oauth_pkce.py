"""Regression test for the PKCE bug that broke YouTube connect on 2026-07-28.

The two halves of an OAuth handshake are two separate HTTP requests, so they
cannot share a Flow object. authorization_url() generates a code_verifier on
one Flow; fetch_token() on a different Flow had none, and Google rejected the
exchange with "invalid_grant: Missing code verifier".

Skips when google-auth-oauthlib is absent (it is not in the test sandbox); runs
for real anywhere the app itself can run.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import google_auth_oauthlib  # noqa: F401
    HAVE_GOOGLE = True
except ImportError:
    HAVE_GOOGLE = False


@unittest.skipUnless(HAVE_GOOGLE, "google-auth-oauthlib not installed")
class PkceCarryOverTests(unittest.TestCase):

    def setUp(self):
        from hub import oauth
        self.oauth = oauth
        oauth._pending_states.clear()
        oauth._pending_verifiers.clear()

    def _fake_flow(self, verifier="verifier-abc", state="state-xyz"):
        flow = mock.Mock()
        flow.code_verifier = verifier
        flow.authorization_url.return_value = ("https://accounts.google/auth", state)
        return flow

    def test_verifier_survives_between_the_two_requests(self):
        """The verifier from leg 1 must reach leg 2, or Google rejects the code."""
        first = self._fake_flow()
        with mock.patch.object(self.oauth, "_google_flow", return_value=first):
            self.oauth.youtube_auth_url()

        self.assertEqual(self.oauth._pending_verifiers["state-xyz"], "verifier-abc")

        second = self._fake_flow(verifier=None)  # a fresh Flow starts with none
        second.credentials = mock.Mock(
            token="t", refresh_token="r", token_uri="u",
            client_id="c", client_secret="s", scopes=None, expiry=None)
        with mock.patch.object(self.oauth, "_google_flow", return_value=second):
            self.oauth.youtube_exchange("the-code", "state-xyz")

        # The exchange Flow was handed the verifier before fetch_token ran.
        self.assertEqual(second.code_verifier, "verifier-abc")
        second.fetch_token.assert_called_once_with(code="the-code")

    def test_verifier_is_consumed_so_a_code_cannot_be_replayed(self):
        first = self._fake_flow()
        with mock.patch.object(self.oauth, "_google_flow", return_value=first):
            self.oauth.youtube_auth_url()

        second = self._fake_flow(verifier=None)
        second.credentials = mock.Mock(
            token="t", refresh_token="r", token_uri="u",
            client_id="c", client_secret="s", scopes=None, expiry=None)
        with mock.patch.object(self.oauth, "_google_flow", return_value=second):
            self.oauth.youtube_exchange("the-code", "state-xyz")

        self.assertNotIn("state-xyz", self.oauth._pending_verifiers)

    def test_auth_url_registers_the_state_for_the_csrf_check(self):
        first = self._fake_flow()
        with mock.patch.object(self.oauth, "_google_flow", return_value=first):
            self.oauth.youtube_auth_url()
        self.assertTrue(self.oauth.check_state("state-xyz", "youtube"))


if __name__ == "__main__":
    unittest.main()
