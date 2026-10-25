import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from src.collector_receiver import ensure_receiver, receiver_health

ROOT = Path(__file__).resolve().parents[1]

class CollectorReceiverTests(unittest.TestCase):
    def setUp(self):
        self.server, self.status = ensure_receiver(ROOT, 0)
        self.port = self.server.server_port
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_owned_receiver_starts_and_health_matches_root(self):
        self.assertEqual(self.status['state'], 'STARTED')
        self.assertTrue(receiver_health(ROOT, self.port))
        self.assertFalse(receiver_health(ROOT / 'different-project', self.port))
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_existing_receiver_is_reused_not_owned(self):
        server, status = ensure_receiver(ROOT, self.port)
        self.assertIsNone(server)
        self.assertEqual(status['state'], 'REUSED')
        self.assertTrue(receiver_health(ROOT, self.port))

    def test_foreign_workspace_is_not_adopted(self):
        server, status = ensure_receiver(ROOT / 'different-project', self.port)
        self.assertIsNone(server)
        self.assertFalse(status['ok'])
        self.assertEqual(status['state'], 'PORT_UNAVAILABLE')
        self.assertTrue(receiver_health(ROOT, self.port))

    def test_collection_requires_trusted_origin_and_no_other_routes(self):
        for path, code in [('/api/collect', 403), ('/api/publish/batch', 404)]:
            request = Request(f'http://127.0.0.1:{self.port}{path}', data=b'{}', headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as error:
                self.opener.open(request, timeout=2)
            self.assertEqual(error.exception.code, code)

if __name__ == '__main__':
    unittest.main()
