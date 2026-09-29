import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src import store_management
from src.store_management import authorization_url, save_oauth_configuration, save_store_preferences, set_active_store, store_status


class StoreManagementTests(unittest.TestCase):
    def test_status_never_returns_token_and_reports_saved_vault(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".secrets").mkdir()
            (root / ".secrets" / "mercadolibre_tokens.dat").write_bytes(b"encrypted")
            result = store_status(root)
            self.assertTrue(result["token_saved"])
            self.assertEqual(result["status"], "SAVED_UNVERIFIED")
            self.assertNotIn("access_token", result)
            self.assertNotIn("refresh_token", result)

    def test_live_status_returns_only_safe_account_summary(self):
        class Stub:
            def me(self):
                return {"id": 123, "nickname": "SAFE_ACCOUNT", "status": {"sell": {"allow": True}}, "tags": []}
            def marketplace_mapping(self, _merchant_id):
                return {"business_model": "CBT", "marketplaces": [{"user_id": 456, "site_id": "MLB", "logistic_type": "remote"}]}
            def listing_capacity(self, _merchant_id):
                return {}
            def items(self, _merchant_id, _limit):
                return {"paging": {"total": 2}}
            def orders(self, _limit):
                return {"paging": {"total": 1}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".secrets").mkdir()
            (root / ".secrets" / "mercadolibre_tokens.dat").write_bytes(b"encrypted")
            result = store_status(root, live=True, client=Stub())
            self.assertEqual(result["status"], "CONNECTED")
            self.assertEqual(result["merchant_id"], 123)
            self.assertEqual(result["marketplaces"][0]["site_id"], "MLB")
            self.assertNotIn("access_token", result)

    def test_preferences_validate_authorization_type(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = save_store_preferences(root, "巴西店", "MLB")
            self.assertEqual(result["alias"], "巴西店")
            with self.assertRaises(ValueError):
                save_store_preferences(root, "坏站点", "BAD")

    def test_oauth_config_is_encrypted_and_secret_is_never_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = save_oauth_configuration(root, {
                "client_id": "123456",
                "client_secret": "not-a-real-secret",
                "redirect_uri": "http://127.0.0.1:8789/oauth/mercadolibre/callback",
            })
            secret_file = root / ".secrets" / "mercadolibre_oauth_config.dat"
            self.assertTrue(public["complete"])
            self.assertTrue(public["has_client_secret"])
            self.assertNotIn("client_secret", public)
            self.assertNotIn(b"not-a-real-secret", secret_file.read_bytes())

    def test_each_new_store_gets_its_own_pending_authorization_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(store_management, "oauth_configuration", return_value={"complete": True, "missing": []}), \
                 mock.patch.object(store_management, "get_runtime_oauth_config", return_value={"client_id": "123", "client_secret": "secret", "redirect_uri": "https://example.test/oauth/mercadolibre/callback"}), \
                 mock.patch.object(store_management.oauth, "build_authorization_url", return_value="https://example.test/auth") as build:
                authorization_url(root, "店铺 A", "MLM")
                authorization_url(root, "店铺 B", "MLB")
            first = build.call_args_list[0].kwargs
            second = build.call_args_list[1].kwargs
            self.assertNotEqual(first["pending_file"], second["pending_file"])
            self.assertNotEqual(first["context"]["store_id"], second["context"]["store_id"])
            self.assertEqual(first["context"]["alias"], "店铺 A")

    def test_active_store_must_exist_and_have_a_saved_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".secrets").mkdir()
            (root / ".secrets" / "mercadolibre_tokens_store2.dat").write_bytes(b"encrypted")
            (root / "config").mkdir()
            (root / "config" / "mercadolibre_stores.json").write_text('{"active_store_id":"legacy","stores":[{"id":"legacy","alias":"旧店"},{"id":"store2","alias":"新店"}]}', encoding="utf-8")
            result = set_active_store(root, "store2")
            self.assertEqual(result["active_store_id"], "store2")


if __name__ == "__main__":
    unittest.main()
