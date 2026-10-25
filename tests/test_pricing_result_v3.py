import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import pricing_result_v3 as pricing
from src.procurement_v3 import digest

ROOT = Path(__file__).resolve().parents[1]
SID = 'MLM12345678'


class PricingResults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        self.policy = json.loads((ROOT / 'config/autopricing_policy.json').read_text('utf-8'))
        self.write_policy()
        self.draft = {'payload': {'sites_to_sell': [{'site_id': 'MLM'}]}, 'input_requests': [{'origin': 'OTHER', 'code': 'KEEP'}],
                      'evidence': {'sku_details': [{'sku_id': SID, 'purchase_cost_cny': '999', 'site_pricing': [{'net_proceeds': '999'}]}],
                                   'zying_detail': {'source': 'zying_detail', 'site_id': 'MLM', 'sku_id': SID,
                                                    'buyer_price': {'USD': 100}, 'net_proceeds': {'USD': 80}, 'commission_pct': 18,
                                                    'package': {'source': 'zying_detail', 'length_cm': 14, 'width_cm': 13, 'height_cm': 12, 'weight_g': 550}},
                                   'source_offer': {'rows': [{'sku_id': SID, 'source_offer': {
                                       'status': 'CANDIDATE', 'state': 'CANDIDATE', 'source_sku_id': 'source-a',
                                       'source_url': 'https://detail.1688.com/offer/123456789.html', 'capture_sha256': 'synthetic-hash',
                                       'cost_basis': 'PER_SELLING_UNIT', 'purchase_cost_cny': '10'}}]}}}
        self.budget = {SID: {'currency': 'USD', 'cny_per_currency': '7', 'domestic_shipping_cny': '2'}}
        self.result = json.loads((ROOT / 'tests/fixtures/pricing_result.json').read_text('utf-8'))

    def write_policy(self):
        (self.root / 'config/autopricing_policy.json').write_text(json.dumps(self.policy), encoding='utf-8')

    def context(self):
        return pricing.inputs(self.root, self.draft, self.draft['evidence']['sku_details'][0], self.budget[SID])

    def bound(self):
        return {**self.result, 'input_sha256': digest(self.context())}

    def run_stage(self, captured=None):
        return pricing.prepare(self.root, self.draft, self.budget, {SID: captured} if captured else None)

    def row(self, captured=None):
        return self.run_stage(captured)['evidence']['pricing_result']['rows'][0]

    def test_captured_zying_available(self):
        row = self.row(self.bound())
        self.assertEqual(row['provider'], 'zying_ai')
        self.assertTrue(row['floor_passed'])
        self.assertEqual(row['status'], 'BUDGET_ESTIMATE')

    def test_1688_result(self):
        self.assertEqual(self.row({**self.bound(), 'provider': '1688_pricing'})['provider'], '1688_pricing')

    def test_unavailable_fallback(self):
        with patch.object(pricing.market_follow_v3, 'solve', wraps=pricing.market_follow_v3.solve) as solve:
            row = self.row({'unavailable': True})
        solve.assert_called_once()
        self.assertEqual(row['provider'], 'LOCAL_FORMULA')
        self.assertTrue(row['floor_passed'])
        self.assertEqual(row['inputs']['purchase_cny'], '10')

    def test_legacy_unverified_reference_falls_back(self):
        self.draft['evidence']['zying_profit_reference'] = {'basis_verified': False, 'reference_only': True, 'margin_hint': 'PASS', 'comparison_margin_pct': 90}
        self.assertEqual(self.row()['provider'], 'LOCAL_FORMULA')

    def test_equal_ten_rejected(self):
        result = self.run_stage({**self.bound(), 'unit_contribution': '10'})
        row = result['evidence']['pricing_result']['rows'][0]
        self.assertFalse(row['floor_passed'])
        self.assertEqual(row['status'], 'SKIPPED')
        self.assertIn('PRICING_BELOW_FLOOR', [r['code'] for r in result['input_requests']])

    def test_exact_before_display_rounding(self):
        self.assertTrue(self.row({**self.bound(), 'unit_contribution': '10.000001'})['floor_passed'])
        self.assertFalse(self.row({**self.bound(), 'unit_contribution': '9.999999'})['floor_passed'])

    def test_local_exact_ten_rejected(self):
        self.policy.update(ads_net_pct='0', tax_reserve_net_pct='0', returns_base_pct='0', packing_service_cny='0')
        self.write_policy()
        self.budget[SID].update(cny_per_currency='1', domestic_shipping_cny='0')
        self.draft['evidence']['source_offer']['rows'][0]['source_offer']['purchase_cost_cny'] = '70'
        self.assertFalse(self.row()['floor_passed'])

    def test_missing_cost_requests_even_if_result_available(self):
        captured = self.bound()
        del self.draft['evidence']['source_offer']['rows'][0]['source_offer']['purchase_cost_cny']
        result = self.run_stage(captured)
        self.assertEqual(result['evidence']['pricing_result']['rows'][0]['decision'], 'BLOCKED_EVIDENCE')
        self.assertTrue(any(r.get('field') == 'source_offer.purchase_cost_cny' for r in result['input_requests']))

    def test_slice2_current_shape_missing_cost(self):
        offer = self.draft['evidence']['source_offer']['rows'][0]['source_offer']
        del offer['cost_basis']
        del offer['purchase_cost_cny']
        self.assertEqual(self.row()['status'], 'SKIPPED')

    def test_commission_conflict_captured_selected(self):
        self.draft['evidence']['zying_detail']['commission_pct'] = 20
        commission = self.row()['inputs']['commission']
        self.assertEqual(commission['selected_pct'], '20')
        self.assertEqual(commission['default_pct'], '18')
        self.assertTrue(commission['conflict'])

    def test_net_no_repeat_commission(self):
        old = self.row()['unit_contribution']
        self.draft['evidence']['zying_detail']['commission_pct'] = 30
        self.assertEqual(self.row()['unit_contribution'], old)

    def test_commission_default_and_gross_formula(self):
        detail = self.draft['evidence']['zying_detail']
        detail.update(commission_pct=None, net_proceeds={})
        self.budget[SID]['platform_shipping'] = '5'
        row = self.row()
        self.assertEqual(row['inputs']['net_proceeds'], '77.00')
        self.assertEqual(row['inputs']['commission']['selected_pct'], '18')

    def test_missing_shipping_not_assumed_zero(self):
        self.draft['evidence']['zying_detail']['net_proceeds'] = {}
        self.assertEqual(self.row()['status'], 'SKIPPED')

    def test_package_only_from_zying(self):
        self.draft['evidence']['sku_details'][0]['package'] = {'weight_g': 999999}
        self.assertEqual(self.row()['inputs']['package']['weight_g'], '550')
        del self.draft['evidence']['zying_detail']['package']['weight_g']
        self.assertEqual(self.row()['status'], 'SKIPPED')

    def test_stale_result_input_rejected(self):
        captured = self.bound()
        self.budget[SID]['domestic_shipping_cny'] = '3'
        self.assertEqual(self.row(captured)['provider'], 'LOCAL_FORMULA')

    def test_unverified_result_basis_rejected(self):
        self.assertEqual(self.row({**self.bound(), 'basis_verified': False})['provider'], 'LOCAL_FORMULA')
        self.assertEqual(self.row({**self.bound(), 'margin_basis': 'NET_PROCEEDS'})['provider'], 'LOCAL_FORMULA')

    def test_input_unchanged_requests_idempotent(self):
        before = copy.deepcopy(self.draft)
        result = self.run_stage()
        self.assertEqual(self.draft, before)
        self.assertEqual(result['evidence']['sku_details'], before['evidence']['sku_details'])
        self.assertEqual(result['payload'], before['payload'])
        self.assertEqual(result, pricing.prepare(self.root, result, self.budget))
        self.assertEqual(result['input_requests'][0]['origin'], 'OTHER')

    def test_negative_and_nonfinite(self):
        self.assertFalse(self.row({**self.bound(), 'unit_contribution': '-1'})['floor_passed'])
        self.assertEqual(self.row({**self.bound(), 'unit_contribution': 'NaN'})['provider'], 'LOCAL_FORMULA')

    def test_currency_mismatch(self):
        self.assertEqual(self.row({**self.bound(), 'currency': 'MXN'})['provider'], 'LOCAL_FORMULA')

    def test_mxn_formula_uses_explicit_exchange_rate(self):
        self.budget[SID]['currency'] = 'MXN'
        self.budget[SID]['cny_per_currency'] = '.4'
        self.draft['evidence']['zying_detail'].update(buyer_price={'MXN': 100}, net_proceeds={'MXN': 80})
        self.assertEqual(self.row()['currency'], 'MXN')

    def test_other_site_and_overweight(self):
        self.draft['evidence']['zying_detail']['site_id'] = 'MLB'
        self.assertEqual(self.row()['status'], 'SKIPPED')
        self.draft['evidence']['zying_detail']['site_id'] = 'MLM'
        self.draft['evidence']['zying_detail']['package']['weight_g'] = 15001
        self.assertEqual(self.row()['status'], 'SKIPPED')

    def test_multi_sku_no_leak_and_continue(self):
        self.draft['evidence']['sku_details'].append({'sku_id': 'second'})
        self.draft['evidence']['sku_details'][0]['market_reference'] = {'market_item_id': SID, 'site_id': 'MLM'}
        rows = self.run_stage()['evidence']['pricing_result']['rows']
        self.assertTrue(rows[0]['floor_passed'])
        self.assertEqual(rows[1]['status'], 'SKIPPED')

    def test_policy_pending_blocks(self):
        self.policy['margin_policy_status'] = 'PENDING_CONFIRMATION'
        self.write_policy()
        self.assertEqual(self.row()['decision'], 'BLOCKED_EVIDENCE')


if __name__ == '__main__':
    unittest.main()
