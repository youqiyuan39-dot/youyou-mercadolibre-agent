import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import offer_search_v3 as offers
from src import product_match_v3
from src.source_intake import normalize_collection, build_processing_draft

ROOT = Path(__file__).resolve().parents[1]


class OfferSearchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'tests/fixtures/offer_candidates.json').read_text('utf-8'))
        self.candidates = self.fixture['candidates']
        self.draft = {'payload': {'pictures': [], 'title': 'preserved'}, 'human_approved': False,
                      'input_requests': [{'code': 'OTHER_STAGE', 'sku_ids': ['T1']}],
                      'evidence': {'market_reference': copy.deepcopy(self.fixture['market_reference']),
                                   'sku_details': [{'sku_id': 'T1', 'offer_match_identity': copy.deepcopy(self.fixture['target_identity']),
                                                    'purchase_cost_cny': None, 'package': {'status': 'UNVERIFIED'}}]}}

    def run_match(self, candidates=None, draft=None, root=ROOT):
        return offers.prepare(root, draft or self.draft, offers.Manual1688Capture(self.candidates if candidates is None else candidates))

    def row(self, result):
        return result['evidence']['source_offer']['rows'][0]

    def codes(self, result):
        return {r['code'] for r in result['input_requests']}

    def test_fixture_selects_positive_skips_negative_and_conflicts(self):
        result = self.run_match()
        row = self.row(result)
        self.assertEqual(row['source_offer']['offer_id'], '900000001')
        self.assertEqual([r['status'] for r in row['candidates']], ['CANDIDATE', 'SKIPPED', 'SKIPPED', 'SKIPPED'])
        self.assertEqual(row['candidates'][2]['state'], 'CONFLICT')
        self.assertTrue({'OFFER_SPEC_CONFLICT', 'OFFER_QUANTITY_CONFLICT', 'OFFER_PACKAGE_CONFLICT', 'OFFER_TITLE_LOW'}.issubset(self.codes(result)))
        self.assertIn('OTHER_STAGE', self.codes(result))

    def test_missing_image_is_null_penalized_and_never_confirmed(self):
        result = self.run_match([self.candidates[0]])
        source = self.row(result)['source_offer']
        self.assertIsNone(source['scores']['image'])
        self.assertNotIn('image', source['effective_weights'])
        self.assertAlmostEqual(sum(source['effective_weights'].values()), 1, places=5)
        self.assertEqual(source['confidence'], 0.9)
        self.assertEqual(source['state'], 'CANDIDATE')
        self.assertTrue(source['requires_human_confirmation'])
        self.assertFalse(source['supplier_dims_validated'])
        self.assertEqual(result['evidence']['source_offer']['status'], 'BLOCKED_EVIDENCE')
        self.assertIn('OFFER_HUMAN_CONFIRMATION_REQUIRED', self.codes(result))

    def test_source_facts_and_original_are_unchanged(self):
        original = copy.deepcopy(self.draft)
        result = self.run_match()
        self.assertEqual(self.draft, original)
        self.assertEqual(result['payload'], original['payload'])
        self.assertEqual(result['evidence']['sku_details'], original['evidence']['sku_details'])
        self.assertEqual(result['evidence']['market_reference'], original['evidence']['market_reference'])
        self.assertFalse(result['human_approved'])

    def test_low_threshold_routes_to_input_requests(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            p = offers.policy(ROOT)
            p['min_confidence'] = 0.95
            (root / 'config').mkdir()
            (root / 'config/offer_match_policy.json').write_text(json.dumps(p), encoding='utf-8')
            result = self.run_match([self.candidates[0]], root=root)
            self.assertIsNone(self.row(result)['source_offer'])
            self.assertIn('OFFER_CONFIDENCE_LOW', self.codes(result))

    def test_tolerance_boundary_and_configurable_rejection(self):
        candidate = copy.deepcopy(self.candidates[0])
        candidate['capture']['skus'][0]['package']['weight_g'] = 660
        self.assertEqual(self.row(self.run_match([candidate]))['status'], 'CANDIDATE')
        candidate['capture']['skus'][0]['package']['weight_g'] = 660.01
        self.assertIn('OFFER_PACKAGE_CONFLICT', self.codes(self.run_match([candidate])))

    def test_missing_package_cannot_pass_high_text_score(self):
        candidate = copy.deepcopy(self.candidates[0])
        candidate['capture']['skus'][0]['package'].pop('height_cm')
        result = self.run_match([candidate])
        self.assertIsNone(self.row(result)['source_offer'])
        self.assertIn('OFFER_PACKAGE_MISSING', self.codes(result))

    def test_missing_specs_and_quantity_are_not_invented(self):
        self.draft['evidence']['sku_details'][0]['offer_match_identity']['specs'] = {}
        self.draft['evidence']['sku_details'][0]['offer_match_identity'].pop('units_per_pack')
        self.draft['evidence']['market_reference'].update(title='filter', units_per_pack=None)
        result = self.run_match([self.candidates[0]])
        self.assertTrue({'OFFER_SPEC_MISSING', 'OFFER_QUANTITY_MISSING'}.issubset(self.codes(result)))
        self.assertIsNone(self.row(result)['source_offer'])

    def test_empty_list_needs_capture(self):
        self.assertIn('OFFER_CANDIDATES_MISSING', self.codes(self.run_match([])))

    def test_invalid_candidate_does_not_block_good_candidate(self):
        result = self.run_match([{'capture': {'title': 'broken'}}, self.candidates[0]])
        self.assertIsNotNone(self.row(result)['source_offer'])
        self.assertIn('OFFER_CAPTURE_INVALID', self.codes(result))

    def test_duplicate_candidate_sku_ids_are_rejected(self):
        c = copy.deepcopy(self.candidates[0])
        c['capture']['skus'].append(copy.deepcopy(c['capture']['skus'][0]))
        self.assertIn('OFFER_CAPTURE_INVALID', self.codes(self.run_match([c])))

    def test_multi_sku_failure_is_isolated(self):
        first = self.draft['evidence']['sku_details'][0]
        first['market_reference'] = copy.deepcopy(self.fixture['market_reference'])
        second = copy.deepcopy(first)
        second['sku_id'] = 'T2'
        second['offer_match_identity']['specs']['model'] = 'VF9999'
        self.draft['evidence']['sku_details'].insert(0, second)
        result = self.run_match([self.candidates[0]])
        rows = result['evidence']['source_offer']['rows']
        self.assertEqual([r['status'] for r in rows], ['SKIPPED', 'CANDIDATE'])
        conflict = next(r for r in result['input_requests'] if r['code'] == 'OFFER_SPEC_CONFLICT')
        self.assertEqual(conflict['sku_ids'], ['T2'])

    def test_shared_reference_cannot_bind_all_variants(self):
        self.draft['evidence']['sku_details'].append({'sku_id': 'T2'})
        result = self.run_match([self.candidates[0]])
        self.assertEqual([r['status'] for r in result['evidence']['source_offer']['rows']], ['SKIPPED', 'SKIPPED'])

    def test_other_market_is_rejected_without_provider_call(self):
        self.draft['evidence']['market_reference']['site_id'] = 'MLB'
        provider = offers.Manual1688Capture(self.candidates)
        with patch.object(provider, 'search', side_effect=AssertionError('should not search')):
            result = offers.prepare(ROOT, self.draft, provider)
        self.assertIn('OFFER_INPUT_INVALID', self.codes(result))

    def test_zying_detail_package_and_listener_reference_are_supported(self):
        self.draft['evidence']['zying_detail'] = {
            'sku_id': 'MLM1234567', 'site_id': 'MLM', 'title': self.fixture['market_reference']['title'],
            'package': self.fixture['target_identity']['package']}
        self.draft['evidence']['sku_details'][0]['offer_match_identity'].pop('package')
        # Existing market_reference.js lead has no package. Resolve matching detail.
        self.assertIsNotNone(self.row(self.run_match([self.candidates[0]]))['source_offer'])
        self.draft['evidence'].pop('market_reference')
        self.assertIsNotNone(self.row(self.run_match([self.candidates[0]]))['source_offer'])

    def test_detail_for_another_market_item_is_not_reused(self):
        self.draft['evidence']['sku_details'][0]['offer_match_identity'].pop('package')
        self.draft['evidence']['zying_detail'] = {'sku_id': 'MLM7654321', 'package': self.fixture['target_identity']['package']}
        self.assertIn('OFFER_PACKAGE_MISSING', self.codes(self.run_match([self.candidates[0]])))

    def test_existing_procurement_gate_is_not_bypassed(self):
        self.draft['evidence']['sku_details'][0]['procurement'] = {'components': []}
        result = self.run_match([self.candidates[0]])
        self.assertIn('COMPOSITION_INVALID', self.codes(result))
        self.assertIsNone(self.row(result)['source_offer'])

    def test_product_match_flags_remain_pending_even_for_human_lead(self):
        with patch.object(offers, 'assess_product', wraps=product_match_v3.assess) as check:
            self.run_match([self.candidates[0]])
        pending = check.call_args.args[2]
        self.assertEqual(pending['spec_match'], 'SOURCE_SELECTED_PENDING_REVIEW')
        self.assertEqual(pending['source_sku_id'], 'S1')

    def test_no_network_browser_model_or_import_calls(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             patch('src.source_intake.save_collection_and_enqueue', side_effect=AssertionError('save forbidden')), \
             patch('src.supplier_api_v3.import_product_get', side_effect=AssertionError('import forbidden')):
            self.assertIsNotNone(self.row(self.run_match([self.candidates[0]]))['source_offer'])

    def test_blocked_provider_acl_and_login_without_requests(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            root = Path(folder)
            provider = offers.Zying1688ImageSearch(root)
            self.assertEqual(provider.search({})['status'], 'BLOCKED_ON_LOGIN')
            provider.logged_in = True
            self.assertEqual(provider.search({})['status'], 'BLOCKED_ON_ACCESS')
            path = root / 'data/v3/supplier1688/last-result.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'diagnostics': {'provider_code': 'gw.APIACLDecline', 'http_status': 400}}), encoding='utf-8')
            provider.logged_in = False
            response = provider.search({})
            self.assertEqual(response['status'], 'BLOCKED_ON_ACCESS')
            self.assertFalse(response['request_performed'])
            result = offers.prepare(ROOT, self.draft, provider)
            self.assertIn('BLOCKED_ON_ACCESS', self.codes(result))

    def test_provider_can_be_plugged_in_and_block_per_sku(self):
        candidate = self.candidates[0]
        class FixtureProvider(offers.OfferSearchProvider):
            name = 'local_fixture'
            def search(self, query):
                if query['target_sku_id'] == 'T1':
                    return {'status': 'BLOCKED_ON_LOGIN', 'candidates': []}
                return {'status': 'READY', 'candidates': [candidate]}
        first = self.draft['evidence']['sku_details'][0]
        first['market_reference'] = copy.deepcopy(self.fixture['market_reference'])
        second = copy.deepcopy(first)
        second['sku_id'] = 'T2'
        self.draft['evidence']['sku_details'].append(second)
        rows = offers.prepare(ROOT, self.draft, FixtureProvider())['evidence']['source_offer']['rows']
        self.assertEqual([r['status'] for r in rows], ['BLOCKED_ON_LOGIN', 'CANDIDATE'])

    def test_forbidden_fields_rejected_and_original_source_offer_preserved(self):
        with self.assertRaises(ValueError):
            offers.Manual1688Capture([{'token': 'synthetic-not-a-secret'}])
        self.draft['evidence']['source_offer'] = {'state': 'HUMAN_CONFIRMED'}
        original = copy.deepcopy(self.draft)
        with self.assertRaises(ValueError):
            self.run_match()
        self.assertEqual(self.draft, original)

    def test_repeated_prepare_does_not_duplicate_own_requests(self):
        first = self.run_match()
        second = self.run_match(draft=first)
        self.assertEqual(first, second)

    def test_candidate_metadata_cannot_override_raw_pack_conflict(self):
        c = copy.deepcopy(self.candidates[0])
        c['capture']['skus'][0]['variant'] = '单个装'
        result = self.run_match([c])
        self.assertIn('OFFER_QUANTITY_CONFLICT', self.codes(result))
        self.assertIsNone(self.row(result)['source_offer'])

    def test_target_identity_cannot_override_market_pack(self):
        self.draft['evidence']['sku_details'][0]['offer_match_identity']['units_per_pack'] = 1
        self.assertIn('OFFER_INPUT_INVALID', self.codes(self.run_match([self.candidates[0]])))

    def test_duplicate_sku_values_across_offers_keep_offer_scope(self):
        c = copy.deepcopy(self.candidates[0])
        c['capture']['source_url'] = 'https://detail.1688.com/offer/800000001.html'
        result = self.run_match([self.candidates[0], c])
        self.assertEqual(self.row(result)['source_offer']['offer_id'], '800000001')
        self.assertEqual(len(self.row(result)['candidates']), 2)

    def test_multi_variant_capture_is_scored_per_source_sku(self):
        c = copy.deepcopy(self.candidates[0])
        other = copy.deepcopy(c['capture']['skus'][0])
        other.update(sku_id='S2', spec_id='SP2')
        c['capture']['skus'].insert(0, other)
        c['sku_identity']['S2'] = {'specs': {'model': 'VF9999', 'color': 'black'}, 'units_per_pack': 2}
        result = self.run_match([c])
        self.assertEqual(self.row(result)['source_offer']['source_sku_id'], 'S1')
        self.assertEqual([x['status'] for x in self.row(result)['candidates']], ['SKIPPED', 'CANDIDATE'])

    def test_conflicting_spec_aliases_and_duplicate_target_ids_are_rejected(self):
        c = copy.deepcopy(self.candidates[0])
        c['sku_identity']['S1']['specs']['MODEL'] = 'VF9999'
        self.assertIn('OFFER_CAPTURE_INVALID', self.codes(self.run_match([c])))
        self.draft['evidence']['sku_details'].append(copy.deepcopy(self.draft['evidence']['sku_details'][0]))
        result = self.run_match([self.candidates[0]])
        self.assertTrue(all(r['source_offer'] is None for r in result['evidence']['source_offer']['rows']))

    def test_invalid_policy_and_provider_io_failure_fail_closed(self):
        p = offers.policy(ROOT)
        p['weights']['title'] = float('nan')
        with patch('src.offer_search_v3.json.loads', return_value=p):
            with self.assertRaises(ValueError):
                offers.policy(ROOT)
        provider = offers.Manual1688Capture(self.candidates)
        with patch.object(provider, 'search', side_effect=OSError('synthetic unavailable')):
            result = offers.prepare(ROOT, self.draft, provider)
        self.assertIn('OFFER_INPUT_INVALID', self.codes(result))

    def test_existing_intake_and_product_match_fixture_path(self):
        capture = normalize_collection(self.candidates[0]['capture'])
        draft = build_processing_draft(capture, ROOT / 'tests/fixtures/capture-not-written.json', ROOT)
        draft['evidence']['market_reference'] = copy.deepcopy(self.fixture['market_reference'])
        sku = draft['evidence']['sku_details'][0]
        sku['offer_match_identity'] = copy.deepcopy(self.fixture['target_identity'])
        result = self.run_match([self.candidates[0]], draft=draft)
        self.assertEqual(self.row(result)['source_offer']['source_sku_id'], 'S1')
        self.assertEqual(result['evidence']['sku_details'], draft['evidence']['sku_details'])


if __name__ == '__main__':
    unittest.main()
