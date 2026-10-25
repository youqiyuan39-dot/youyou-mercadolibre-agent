import gzip
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from src.public_read_v3 import read, read_json


class PublicReadGzipTests(unittest.TestCase):
    def response(self, data):
        result = io.BytesIO(data)
        result.headers = SimpleNamespace(get_content_type=lambda: 'application/json')
        return result

    def test_gzipped_public_json_is_decoded_before_parsing(self):
        data = gzip.compress(json.dumps({'CBT1': {'name': 'Skin Care'}}).encode())
        with patch('src.public_read_v3.urllib.request.urlopen', return_value=self.response(data)):
            self.assertEqual(read_json('https://api.mercadolibre.com/categories/CBT1')['CBT1']['name'], 'Skin Care')

    def test_curl_fallback_uses_the_same_decoding(self):
        data = gzip.compress(b'{"id":"CBT1"}')
        with (
            patch('src.public_read_v3.urllib.request.urlopen', side_effect=URLError('TLS handshake')),
            patch('src.public_read_v3.shutil.which', return_value='curl.exe'),
            patch('src.public_read_v3.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=data)),
        ):
            self.assertEqual(read_json('https://api.mercadolibre.com/categories/CBT1'), {'id': 'CBT1'})

    def test_decompressed_budget_is_enforced(self):
        data = gzip.compress(b'x' * 50000)
        with patch('src.public_read_v3.urllib.request.urlopen', return_value=self.response(data)):
            with self.assertRaisesRegex(OSError, '超过读取预算'):
                read('https://api.mercadolibre.com/sites/CBT/categories/all', limit=100)

    def test_plain_json_and_image_bytes_are_unchanged(self):
        for data in (b'{"ok":true}', b'\xff\xd8example-jpeg'):
            with self.subTest(data=data):
                with patch('src.public_read_v3.urllib.request.urlopen', return_value=self.response(data)):
                    self.assertEqual(read('https://api.mercadolibre.com/test')[0], data)

    def test_truncated_gzip_is_a_transport_failure(self):
        data = gzip.compress(b'{"ok":true}')[:12]
        with patch('src.public_read_v3.urllib.request.urlopen', return_value=self.response(data)):
            with self.assertRaisesRegex(OSError, '压缩响应不完整'):
                read_json('https://api.mercadolibre.com/test')


if __name__ == '__main__':
    unittest.main()
