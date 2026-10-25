import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from src.official_budget_evidence_v3 import (
    validate_quote, evaluate_budget, COST_KEYS, SOURCE_TYPE, SOURCE_URL)


class OfficialBudgetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = b'Synthetic unit-test artifact, not a real official quote.'
        (self.root / 'evidence.txt').write_bytes(source)
        self.quote = {
            'schema_version': 1, 'source_type': SOURCE_TYPE, 'source_url': SOURCE_URL,
            'observed_at': '2026-10-22T10:00:00+08:00',
            'evidence_files': [{'path': 'evidence.txt', 'sha256': hashlib.sha256(source).hexdigest()}],
            'binding': {'draft_name': 'test-draft.json', 'sku_id': 'test-sku',
                        'store_id': 'test-store', 'source_capture_sha256': 'a' * 64},
            'inputs': {'origin_country': 'CN', 'site_id': 'MLM', 'logistic_type': 'remote',
                       'category_id': 'MLM123', 'category_path': ['Tools', 'Test'],
                       'listing_type_id': 'gold_special', 'currency_id': 'USD',
                       'listing_price': '100.00', 'free_shipping': True,
                       'package': {'weight_g': '650', 'length_cm': '26', 'width_cm': '8', 'height_cm': '4'}},
            'outputs': {'currency_id': 'USD', 'selling_fee': '10',
                        'international_shipping_fee': '8', 'local_shipping_fee': '2',
                        'net_proceeds': '80'}}
        self.expected = copy.deepcopy({k: self.quote[k] for k in ('binding', 'inputs')})
        self.policy = {'margin_policy_status': 'CONFIRMED', 'confirmed_margin_basis': 'BUYER_PRICE',
                       'minimum_margin_operator': 'GT', 'minimum_margin_pct': '10'}

    def check(self, quote=None, **kwargs):
        return validate_quote(self.root, quote or self.quote, self.expected,
                              now=kwargs.pop('now', '2026-10-22T10:30:00+08:00'), **kwargs)

    def test_complete_record_is_not_publish_permission(self):
        result = self.check()
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['publication_authorized'])
        self.assertFalse(result['authenticity_verified'])
        self.assertFalse(result['source_authenticity_verified'])
        self.assertIn('not official origin', result['notice'])

    def test_complete_expected_scope_is_independent(self):
        for field, replacement in [('store_id', 'other'), ('sku_id', 'other'),
                                   ('source_capture_sha256', 'b' * 64)]:
            with self.subTest(field=field):
                q = copy.deepcopy(self.quote)
                q['binding'][field] = replacement
                self.assertFalse(self.check(q)['ok'])
        for key in self.expected['inputs']:
            with self.subTest(missing=key):
                q = copy.deepcopy(self.quote)
                del q['inputs'][key]
                self.assertFalse(self.check(q)['ok'])

    def test_changed_price_package_or_category_rejected(self):
        for key, value in [('listing_price', '99'), ('category_id', 'MLM999'),
                           ('category_path', ['Other']), ('free_shipping', False)]:
            with self.subTest(field=key):
                q = copy.deepcopy(self.quote)
                q['inputs'][key] = value
                self.assertFalse(self.check(q)['ok'])
        q = copy.deepcopy(self.quote)
        q['inputs']['package']['weight_g'] = '651'
        self.assertFalse(self.check(q)['ok'])

    def test_time_and_timezone(self):
        for now in ['2026-10-22T09:59:59+08:00', '2026-10-22T11:00:01+08:00', '2026-10-22T10:30:00']:
            with self.subTest(now=now):
                self.assertFalse(self.check(now=now)['ok'])
        self.assertTrue(self.check(now='2026-10-22T11:00:00+08:00')['ok'])
        self.assertFalse(self.check(max_age_seconds=0)['ok'])

    def test_artifact_missing_changed_or_outside_root(self):
        q = copy.deepcopy(self.quote)
        for path in ['missing.txt', '../test_official_budget_evidence_v3.py', str((self.root / 'evidence.txt').resolve())]:
            with self.subTest(path=path):
                q['evidence_files'][0]['path'] = path
                self.assertFalse(self.check(q)['ok'])
        (self.root / 'evidence.txt').write_text('changed', encoding='utf-8')
        self.assertFalse(self.check()['ok'])

    def test_no_proof_and_duplicate_proof_rejected(self):
        q = copy.deepcopy(self.quote)
        q['evidence_files'] = []
        self.assertFalse(self.check(q)['ok'])
        q['evidence_files'] = self.quote['evidence_files'] * 2
        self.assertFalse(self.check(q)['ok'])

    def test_missing_nonfinite_negative_precision_and_currency(self):
        for value in [None, True, 'NaN', 'Infinity', '-1', '0.001']:
            with self.subTest(value=value):
                q = copy.deepcopy(self.quote)
                q['outputs']['selling_fee'] = value
                self.assertFalse(self.check(q)['ok'])
        q = copy.deepcopy(self.quote)
        q['outputs']['currency_id'] = 'MXN'
        self.assertFalse(self.check(q)['ok'])
        del q['outputs']['local_shipping_fee']
        self.assertFalse(self.check(q)['ok'])

    def test_reconciliation_cent_rounding_only(self):
        q = copy.deepcopy(self.quote)
        q['outputs']['net_proceeds'] = '80.02'
        self.assertTrue(self.check(q)['ok'])
        q['outputs']['net_proceeds'] = '80.03'
        self.assertFalse(self.check(q)['ok'])
        q['outputs']['net_proceeds'] = '101'
        self.assertFalse(self.check(q)['ok'])

    def test_strict_floor_and_buyer_denominator(self):
        costs = dict.fromkeys(COST_KEYS, '0')
        for purchase, passed in [('70', False), ('70.00001', False), ('69.99999', True)]:
            with self.subTest(purchase=purchase):
                costs['purchase'] = purchase
                result = evaluate_budget(self.check(), costs, self.policy)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['floor_passed'], passed)
                self.assertFalse(result['publication_authorized'])
                self.assertFalse(result['source_authenticity_verified'])
                self.assertFalse(result['authenticity_verified'])
        costs['purchase'] = '69'
        self.assertEqual(evaluate_budget(self.check(), costs, self.policy)['sales_margin_pct'], '11.00')

    def test_quote_rounding_cannot_manufacture_profit_pass(self):
        q = copy.deepcopy(self.quote)
        q['outputs']['net_proceeds'] = '80.02'
        costs = dict.fromkeys(COST_KEYS, '0')
        costs['purchase'] = '70'
        result = evaluate_budget(self.check(q), costs, self.policy)
        self.assertTrue(result['ok'])
        self.assertFalse(result['floor_passed'])

    def test_unknown_costs_and_double_counted_platform_fees_rejected(self):
        costs = dict.fromkeys(COST_KEYS, '0')
        for key in COST_KEYS:
            with self.subTest(missing=key):
                bad = dict(costs)
                del bad[key]
                self.assertFalse(evaluate_budget(self.check(), bad, self.policy)['ok'])
        costs['selling_fee'] = '10'
        self.assertFalse(evaluate_budget(self.check(), costs, self.policy)['ok'])

    def test_policy_and_invalid_validation_rejected(self):
        costs = dict.fromkeys(COST_KEYS, '0')
        for key, value in [('minimum_margin_operator', 'GTE'), ('minimum_margin_pct', '9'),
                           ('confirmed_margin_basis', 'NET_PROCEEDS'), ('margin_policy_status', 'PENDING')]:
            with self.subTest(key=key):
                policy = {**self.policy, key: value}
                self.assertFalse(evaluate_budget(self.check(), costs, policy)['ok'])
        self.assertFalse(evaluate_budget({'ok': False}, costs, self.policy)['ok'])


if __name__ == '__main__':
    unittest.main()
