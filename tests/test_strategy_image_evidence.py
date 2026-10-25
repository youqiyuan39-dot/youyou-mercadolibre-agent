"""Public image evidence transport and exact ownership, without model/network calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.source_intake import normalize_collection, build_processing_draft
from src.round2_evidence_v3 import recover

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / 'docs/strategy-loop-20261018'
URL_A = 'https://cbu01.alicdn.com/img/ibank/example-a.jpg'
URL_B = 'https://cbu01.alicdn.com/img/ibank/example-b.jpg'


def capture(evidence):
    return normalize_collection({'source_url': 'https://detail.1688.com/offer/123456789.html',
        'title': 'Two filter variants', 'collection_mode': 'multi',
        'skus': [{'sku_id': 'a', 'spec_id': 'sa', 'variant': 'Model A / single bag'},
                 {'sku_id': 'b', 'spec_id': 'sb', 'variant': 'Model B / single bag'}],
        'sku_image_evidence': evidence})


class ImageEvidenceTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def draft(self, source):
        p = self.root / 'capture.json'
        p.write_text(json.dumps(source), encoding='utf-8')
        return build_processing_draft(source, p, self.root)

    def test_normalization_retains_bounded_public_labels(self):
        result = capture([{'url': URL_A, 'label': 'Model A', 'sku_id': 'a',
            'captured_at': '2026-10-18T00:00:00+00:00', 'unrelated_page_state': 'discard'},
            {'url': 'https://unrelated.invalid/a.jpg', 'label': 'Model B'},
            {'url': URL_B}])
        rows = result['sku_image_evidence']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['images'], [URL_A])
        self.assertEqual(rows[0]['captured_at'], '2026-10-18T00:00:00+00:00')
        self.assertNotIn('unrelated_page_state', rows[0])
        self.assertEqual(len(capture([{'url': URL_A, 'label': str(i)} for i in range(250)])['sku_image_evidence']), 200)

    def test_saved_dom_label_and_spec_id_recover_exact_sku(self):
        source = capture([{'url': URL_A, 'label': 'Model A'}, {'url': URL_B, 'spec_id': 'sb'}])
        draft = self.draft(source)
        actions = recover(self.root, draft)
        groups = draft['evidence']['image_review']['sku_source_galleries']
        self.assertEqual({g['sku_id']: g['gallery'][0]['remote_url'] for g in groups}, {'a': URL_A, 'b': URL_B})
        self.assertEqual(len(actions), 2)
        self.assertEqual(recover(self.root, draft), [])

    def test_conflicting_id_and_common_label_never_assign(self):
        draft = self.draft(capture([{'url': URL_A, 'sku_id': 'unknown', 'label': 'Model A'},
            {'url': URL_B, 'label': 'single bag'}, {'url': URL_A, 'sku_id': 'a', 'spec_id': 'sb'}]))
        recover(self.root, draft)
        self.assertEqual(draft['evidence']['image_review']['sku_source_galleries'], [])

    def test_duplicate_variant_and_spec_are_ambiguous(self):
        source = capture([{'url': URL_A, 'label': 'same'}, {'url': URL_B, 'spec_id': 'same-spec'}])
        for row in source['skus']:
            row.update(variant='same', spec_id='same-spec')
        draft = self.draft(source)
        recover(self.root, draft)
        self.assertEqual(draft['evidence']['image_review']['sku_source_galleries'], [])

    def test_existing_human_binding_and_final_gallery_preserved(self):
        draft = self.draft(capture([{'url': URL_A, 'sku_id': 'a'}]))
        review = draft['evidence']['image_review']
        review['sku_source_galleries'] = [{'sku_id': 'a', 'gallery': [{'local_file': 'human.jpg', 'review_status': 'HUMAN_SELECTED'}]}]
        review['sku_listing_galleries'] = copy.deepcopy(review['sku_source_galleries'])
        before = copy.deepcopy(review)
        recover(self.root, draft)
        self.assertEqual(review, before)

    @unittest.skipUnless((ROOT/'data/intake/1688-1083330600526-20261017-094142-734916.json').is_file(), 'Private captured fixture is not distributed')
    def test_real_eleven_variant_capture_preserves_original_mapping(self):
        path = ROOT / 'data/intake/1688-1083330600526-20261017-094142-734916.json'
        source = json.loads(path.read_text('utf-8'))
        draft = self.draft(source)
        before = copy.deepcopy(draft['evidence']['image_review']['sku_source_galleries'])
        self.assertEqual(len(before), 11)
        self.assertEqual(len({g['gallery'][0]['remote_url'] for g in before}), 11)
        # Explicitly lost materialization, not a claim that the production draft had lost it.
        draft['evidence']['image_review']['sku_source_galleries'] = []
        recover(self.root, draft)
        after = draft['evidence']['image_review']['sku_source_galleries']
        expected = {g['sku_id']: [i['remote_url'] for i in g['gallery']] for g in before}
        self.assertEqual({g['sku_id']: [i['remote_url'] for i in g['gallery']] for g in after}, expected)


if __name__ == '__main__':
    unittest.main()
