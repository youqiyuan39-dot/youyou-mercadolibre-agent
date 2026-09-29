import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mercadolibre_oauth as oauth


class OAuthTests(unittest.TestCase):
    def test_pkce_is_s256_compatible(self):
        verifier, challenge = oauth.create_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn("=", challenge)
        self.assertEqual(len(challenge), 43)

    def test_authorization_url_has_required_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pending = Path(temp_dir) / "pending.dat"
            with mock.patch.object(oauth, "PENDING_FILE", pending), mock.patch.object(
                oauth, "save_private_json"
            ) as save:
                url = oauth.build_authorization_url("123", "https://example.test/callback")
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                self.assertEqual(query["client_id"], ["123"])
                self.assertEqual(query["response_type"], ["code"])
                self.assertEqual(query["code_challenge_method"], ["S256"])
                save.assert_called_once()

    def test_private_json_uses_protection_layer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secret.dat"
            with mock.patch.object(oauth, "_protect", side_effect=lambda value: value[::-1]), mock.patch.object(
                oauth, "_unprotect", side_effect=lambda value: value[::-1]
            ):
                oauth.save_private_json(path, {"refresh_token": "not-a-real-token"})
                self.assertNotIn(b"not-a-real-token", path.read_bytes())
                self.assertEqual(oauth.load_private_json(path)["refresh_token"], "not-a-real-token")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI only")
    def test_dpapi_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secret.dat"
            try:
                oauth.save_private_json(path, {"refresh_token": "not-a-real-token"})
            except OSError as exc:
                self.skipTest(f"当前运行环境未加载用户 DPAPI 配置：{exc}")
            self.assertNotIn(b"not-a-real-token", path.read_bytes())
            self.assertEqual(oauth.load_private_json(path)["refresh_token"], "not-a-real-token")

    def test_exchange_code_accepts_explicit_encrypted_app_config(self):
        with tempfile.TemporaryDirectory() as folder:
            pending = Path(folder) / "pending.dat"
            token = Path(folder) / "token.dat"
            with mock.patch.object(oauth, "_protect", side_effect=lambda value: value[::-1]), mock.patch.object(
                oauth, "_unprotect", side_effect=lambda value: value[::-1]
            ), mock.patch.object(oauth, "_post_form", return_value={"access_token": "saved"}) as post:
                oauth.save_private_json(pending, {"state": "state", "code_verifier": "verifier"})
                oauth.exchange_code("code", "state", client_id="123", client_secret="secret", redirect_uri="https://example.test/oauth/mercadolibre/callback", pending_file=pending, token_file=token)
                fields = post.call_args.args[1]
                self.assertEqual(fields["client_id"], "123")
                self.assertEqual(fields["client_secret"], "secret")
                self.assertTrue(token.is_file())

    def test_refresh_uses_the_selected_store_token_file(self):
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "store-token.dat"
            with mock.patch.object(oauth, "load_private_json", return_value={"refresh_token": "refresh"}), \
                 mock.patch.object(oauth, "_post_form", return_value={"access_token": "replacement"}) as post, \
                 mock.patch.object(oauth, "save_private_json") as save:
                oauth.refresh_tokens(client_id="123", client_secret="secret", token_file=token)
            self.assertEqual(post.call_args.args[1]["client_id"], "123")
            self.assertEqual(save.call_args.args[0], token)


if __name__ == "__main__":
    unittest.main()
