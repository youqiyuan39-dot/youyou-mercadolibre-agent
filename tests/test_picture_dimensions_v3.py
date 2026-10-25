import hashlib
from pathlib import Path
import tempfile
import unittest

from src import publish_service as service
from tests.test_publish_service import sample_draft, StubClient
from tests import test_target_publish_v3 as fixture


class PictureDimensionTests(unittest.TestCase):
    def test_actual_processed_size_rejects_despite_large_thumbnail_canvas(self):
        with self.assertRaisesRegex(service.PublishError, '470x488'):
            service._validate_picture_dimensions({'max_size': '470x488',
                                                  'variations': [{'size': '800x800'}]})

    def test_one_side_500_is_sufficient(self):
        for size in ('500x280', '280x500', '800x800'):
            service._validate_picture_dimensions({'max_size': size})

    def test_absent_or_malformed_size_is_not_verified(self):
        for size in (None, '', '0x800', '800', 'unknown'):
            with self.subTest(size=size), self.assertRaises(service.PublishError):
                service._validate_picture_dimensions({'max_size': size})

    def test_legacy_id_revalidates_once_and_proven_cache_reuses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'assets').mkdir()
            (root/'assets/product.png').write_bytes(b'picture')
            fingerprint = hashlib.sha256(b'picture').hexdigest()
            cache = {fingerprint: 'legacy-id'}; client = StubClient()
            service._upload_pictures(sample_draft(), root, client, cache)
            service._upload_pictures(sample_draft(), root, client, cache)
            self.assertEqual(len(client.uploads), 1)
            self.assertEqual(cache[fingerprint], '123-CBT456_092026')

    def test_known_bad_size_stops_without_repeated_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'assets').mkdir()
            (root/'assets/product.png').write_bytes(b'picture')
            fingerprint = hashlib.sha256(b'picture').hexdigest()
            cache = {fingerprint: 'bad-id', '__picture_dimensions_v1': {
                fingerprint: {'id': 'bad-id', 'max_size': '470x488'}}}
            client = StubClient()
            with self.assertRaises(service.PublishError):
                service._upload_pictures(sample_draft(), root, client, cache)
            self.assertFalse(client.uploads)


class PictureCommitTests(unittest.TestCase):
    setUp = fixture.TargetPublishTests.setUp
    tearDown = fixture.TargetPublishTests.tearDown
    prepare = fixture.TargetPublishTests.prepare
    def test_small_uploaded_original_never_calls_publication_endpoint(self):
        preview = self.prepare()
        self.client.upload_picture = lambda *args: {'id': 'bad-id', 'max_size': '470x488'}
        result = self.commit_prepared(preview)
        self.assertEqual(result['record']['status'], 'failed')
        self.assertIn('470x488', result['record']['error'])
        self.assertFalse(result['publishing_performed'])
        self.assertFalse(self.client.posts)

    def commit_prepared(self, preview):
        from src import target_publish_v3
        return target_publish_v3.commit(self.root, preview['id'], True, budget_review_confirmed=True)


if __name__ == '__main__':
    unittest.main()
