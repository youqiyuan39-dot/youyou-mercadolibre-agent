import copy
import tempfile
import unittest
from pathlib import Path

from src.source_intake import IntakeError, normalize_collection, build_processing_draft, refresh_draft_from_collection


class SingleSkuCaptureScopeTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            'source_url': 'https://detail.1688.com/offer/1075271613391.html',
            'title': '乳液和精华', 'collection_mode': 'single',
            'pricing': {'amount': 4, 'currency': 'CNY'},
            'package': {}, 'unit': '个',
            'skus': [
                {'sku_id': 'green', 'spec_id': 'green-spec', 'variant': '绿精华', 'price': 4.5,
                 'package': {'weight_g': 30}},
                {'sku_id': 'am', 'spec_id': 'am-spec', 'variant': 'AM乳', 'price': 6,
                 'package': {'weight_g': 31}},
            ],
        }

    def draft(self, payload, root):
        return build_processing_draft(normalize_collection(payload), root / 'capture.json', root)

    def test_explicit_selection_uses_its_own_quote_and_keeps_source_catalog(self):
        self.payload['selected_sku_id'] = 'am'
        with tempfile.TemporaryDirectory() as temp:
            draft = self.draft(self.payload, Path(temp))
        ev = draft['evidence']
        self.assertEqual([s['sku_id'] for s in ev['sku_details']], ['am'])
        self.assertEqual([s['sku_id'] for s in ev['unselected_sku_details']], ['green'])
        self.assertEqual(len(ev['supplier_page']['skus']), 2)
        self.assertEqual(ev['supplier_page']['selected_variant'], ['AM乳'])
        self.assertEqual(draft['pricing_plan']['purchase_cost_cny'], 6)
        self.assertEqual(ev['purchase_cost']['amount'], 6)
        self.assertEqual(ev['packed_weight']['value'], 31)
        self.assertIsNone(ev['packed_dimensions'])

    def test_missing_sku_quote_does_not_borrow_offer_minimum(self):
        self.payload['selected_sku_id'] = 'am'
        self.payload['skus'][1]['price'] = None
        with tempfile.TemporaryDirectory() as temp:
            draft = self.draft(self.payload, Path(temp))
        self.assertIsNone(draft['pricing_plan']['purchase_cost_cny'])
        self.assertIsNone(draft['evidence']['purchase_cost'])

    def test_unknown_duplicate_and_multi_selection_are_rejected(self):
        for kind in ('unknown', 'duplicate', 'multi'):
            with self.subTest(kind=kind):
                payload = copy.deepcopy(self.payload)
                payload['selected_sku_id'] = 'unknown' if kind == 'unknown' else 'am'
                if kind == 'duplicate':
                    payload['skus'].append(copy.deepcopy(payload['skus'][1]))
                if kind == 'multi':
                    payload['collection_mode'] = 'multi'
                with self.assertRaises(IntakeError):
                    normalize_collection(payload)

    def test_switching_scope_retains_human_work_and_multi_can_restore_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = self.draft(self.payload, root)
            green = original['evidence']['sku_details'][0]
            green.update(purchase_cost_cny=99, purchase_cost_status='USER_CONFIRMED',
                         procurement={'locked': True, 'components': [{'source_sku_id': 'green'}]})
            original['evidence']['images_human_reviewed'] = True
            original['evidence']['image_review']['gallery'] = [{'local_file': 'human.jpg'}]
            selected = copy.deepcopy(self.payload)
            selected['selected_sku_id'] = 'am'
            narrowed = refresh_draft_from_collection(original, normalize_collection(selected), root / 'new.json', root)
            self.assertEqual([s['sku_id'] for s in narrowed['evidence']['sku_details']], ['am'])
            archived = narrowed['evidence']['unselected_sku_details'][0]
            self.assertEqual(archived['purchase_cost_cny'], 99)
            self.assertTrue(archived['procurement']['locked'])
            self.assertEqual(narrowed['evidence']['image_review']['gallery'], [{'local_file': 'human.jpg'}])
            self.assertEqual(narrowed['pricing_plan']['purchase_cost_cny'], 6)
            multi = copy.deepcopy(self.payload)
            multi['collection_mode'] = 'multi'
            restored = refresh_draft_from_collection(narrowed, normalize_collection(multi), root / 'multi.json', root)
            self.assertEqual(len(restored['evidence']['sku_details']), 2)
            restored_green = next(s for s in restored['evidence']['sku_details'] if s['sku_id'] == 'green')
            self.assertEqual(restored_green['purchase_cost_cny'], 99)
            self.assertTrue(restored_green['procurement']['locked'])
            self.assertEqual(restored['evidence']['unselected_sku_details'], [])

    def test_legacy_capture_does_not_infer_a_selected_sku(self):
        with tempfile.TemporaryDirectory() as temp:
            draft = self.draft(self.payload, Path(temp))
        self.assertEqual(len(draft['evidence']['sku_details']), 2)
        self.assertEqual(draft['evidence']['processing_scope']['selected_sku_id'], '')


if __name__ == '__main__':
    unittest.main()
