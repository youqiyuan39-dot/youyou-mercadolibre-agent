import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.source_image_resolution_v3 import original_url, recover_image

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://cbu01.alicdn.com/img/ibank/source.jpg_sum.jpg'


@unittest.skipIf(Image is None, 'Pillow verifier is not installed in this runtime')
class ResolutionTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'docs/strategy-loop-20261018'
        folder.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        high = Image.new('RGB', (600, 600), 'white')
        ImageDraw.Draw(high).rectangle((100, 150, 400, 500), fill='#324455')
        def encode(image):
            value = io.BytesIO()
            image.save(value, 'JPEG', quality=95)
            return value.getvalue()
        self.high = encode(high)
        self.thumb = encode(high.resize((80, 80)))

    def test_same_asset_larger_and_cache_prevents_repeated_download(self):
        fetch = Mock(side_effect=[self.thumb, self.high])
        source = {'remote_url': URL, 'sku_id': 'selected'}
        result, trace = recover_image(self.root, source, fetch)
        self.assertEqual(trace['status'], 'RECOVERED')
        self.assertEqual(trace['before_dimensions'], [80, 80])
        self.assertEqual(trace['after_dimensions'], [600, 600])
        self.assertEqual(result['sku_id'], 'selected')
        self.assertEqual(result['remote_url'], URL)
        self.assertNotIn('local_file', source)
        self.assertTrue((self.root / 'assets' / result['local_file']).is_file())
        result2, trace2 = recover_image(self.root, source, fetch)
        self.assertEqual(result2['local_file'], result['local_file'])
        self.assertTrue(trace2['cached'])
        self.assertEqual(fetch.call_count, 2)

    def test_wrong_asset_rejected_and_failure_is_bounded(self):
        other = io.BytesIO()
        Image.new('RGB', (600, 600), 'red').save(other, 'JPEG')
        source = {'remote_url': URL, 'sku_id': 'selected'}
        fetch = Mock(side_effect=[self.thumb, other.getvalue()])
        result, trace = recover_image(self.root, source, fetch)
        self.assertEqual(trace['status'], 'RETAINED_ORIGINAL')
        self.assertEqual(result, source)
        recover_image(self.root, source, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_human_selection_and_local_original_are_never_replaced(self):
        for source in [{'remote_url': URL, 'review_status': 'HUMAN_SELECTED'},
                       {'remote_url': URL, 'local_file': 'existing.jpg'}]:
            fetch = Mock()
            result, trace = recover_image(self.root, source, fetch)
            self.assertEqual(result, source)
            fetch.assert_not_called()

    def test_small_or_unreadable_original_keeps_existing_evidence(self):
        for data in [self.thumb, b'not-an-image', b'x' * 10_000_001]:
            with tempfile.TemporaryDirectory(dir=self.root) as sub:
                source = {'remote_url': URL}
                result, trace = recover_image(Path(sub), source, Mock(side_effect=[self.thumb, data]))
                self.assertEqual(result, source)
                self.assertEqual(trace['status'], 'RETAINED_ORIGINAL')

    def test_screening_receives_verified_original_without_rebinding_source(self):
        from src.source_image_screening_v3 import screen
        source = {'remote_url': URL, 'sku_id': 'a', 'image_type': 'sku'}
        recover_image(self.root, source, Mock(side_effect=[self.thumb, self.high]))
        draft = {'evidence': {'image_review': {'sku_source_galleries': [
            {'sku_id': 'a', 'gallery': [source]}]}}}
        def inspector(root, draft, sku, image):
            with Image.open(root / 'assets' / image['local_file']) as value:
                self.assertEqual(value.size, (600, 600))
            return {'usable': True, 'contains_watermark': False, 'wrong_variant': False,
                    'uncertain': False, 'fingerprint': 'verified-test-image', 'local_file': image['local_file']}
        screen(self.root, draft, [{'sku_id': 'a'}], inspector=inspector)
        review = draft['evidence']['image_review']
        self.assertEqual(review['sku_source_galleries'][0]['gallery'], [source])
        self.assertIn('local_file', review['sku_listing_galleries'][0]['gallery'][0])
        self.assertEqual(review['source_screening']['skus'][0]['items'][0]['source_resolution']['status'], 'RECOVERED')


class URLTests(unittest.TestCase):
    def test_only_exact_alibaba_thumbnail_path_is_recovered(self):
        self.assertEqual(original_url(URL), URL[:-8])
        for value in ['https://evil.invalid/source.jpg_sum.jpg', 'https://alicdn.com.evil.invalid/a.jpg_sum.jpg',
                      'https://cbu01.alicdn.com/a.jpg?size=80', 'http://cbu01.alicdn.com/a.jpg_sum.jpg',
                      'https://cbu01.alicdn.com/a.jpg_sum.jpg?token=private', 'https://cbu01.alicdn.com/a.jpg']:
            self.assertIsNone(original_url(value))


if __name__ == '__main__':
    unittest.main()
