import base64
import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from src.draft_editor import YOUYOU_EXTENSION_ORIGIN, make_handler


class PortabilityTests(unittest.TestCase):
    def test_extension_id_is_stable_and_matches_server_origin(self):
        manifest_path = Path(__file__).resolve().parents[1] / "collector_extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        public_key = base64.b64decode(manifest["key"], validate=True)
        digest = hashlib.sha256(public_key).digest()[:16]
        extension_id = "".join(chr(ord("a") + nibble) for byte in digest for nibble in (byte >> 4, byte & 15))
        self.assertEqual(YOUYOU_EXTENSION_ORIGIN, f"chrome-extension://{extension_id}")

    def test_local_server_accepts_collector_preflight_and_rejects_foreign_post(self):
        with TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Path(directory)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                preflight = urllib.request.Request(
                    base + "/api/collect", method="OPTIONS",
                    headers={"Origin": YOUYOU_EXTENSION_ORIGIN},
                )
                with urllib.request.urlopen(preflight) as response:
                    self.assertEqual(response.status, 204)
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], YOUYOU_EXTENSION_ORIGIN)
                foreign = urllib.request.Request(
                    base + "/api/model-settings", data=b"{}", method="POST",
                    headers={"Origin": "https://foreign.example", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(foreign)
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
                self.assertFalse((Path(directory) / "config" / "model_settings.json").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
