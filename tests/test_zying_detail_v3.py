import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

from src import zying_detail_v3 as z

FIXTURE = Path(__file__).parent / 'fixtures' / 'zying_detail_test.json'


class ZyingDetailTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def test_all_fields_and_units(self):
        result = z.normalize(self.payload)
        self.assertEqual([result['package'][k] for k in z.FIELDS], [32, 20, 18, 2500])
        self.assertEqual(result['buyer_price'], {'MXN': 1200.5, 'USD': 60.25})
        self.assertEqual(result['net_proceeds'], {'MXN': 900, 'USD': 45})
        self.assertEqual(result['volumetric_weight_g'], 3000)
        self.assertEqual(result['commission_pct'], 15.5)
        self.assertEqual(result['sales'], 1234)
        for key in ('seller', 'fulfillment', 'category_path', 'listed_at', 'title'):
            self.assertEqual(result[key], self.payload['fields'][key])
        self.assertEqual(result['main_image'], self.payload['main_image'])
        self.assertEqual(result['status'], 'CAPTURE_READY')

    def test_packaging_evidence_and_raw_provenance(self):
        normalized = z.normalize(self.payload)
        draft = z.build_draft(normalized)
        ev = draft['evidence']
        self.assertEqual(ev['packed_weight']['value'], 2500)
        self.assertEqual(ev['packed_dimensions']['length'], 32)
        for evidence in (ev['packed_weight'], ev['packed_dimensions'], ev['sku_details'][0]['package']['evidence']):
            self.assertEqual(evidence['source'], 'zying_detail')
            self.assertEqual(evidence['sku_id'], self.payload['sku_id'])
            self.assertEqual(evidence['raw']['weight'], '2.5 kg')
            self.assertEqual(evidence['source_url'], self.payload['source_url'])
            self.assertTrue(evidence['captured_at'])

    def test_missing_dimensions_and_weight_request_evidence(self):
        for key, missing_count in (('dimensions', 3), ('weight', 1)):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                payload['fields'][key] = ''
                result = z.normalize(payload)
                self.assertEqual(result['status'], 'BLOCKED_EVIDENCE')
                self.assertEqual(len([r for r in result['input_requests'] if r.get('blocking') is not False]), missing_count)
                self.assertEqual(len([r for r in result['input_requests'] if r.get('code') == 'ZYING_PROFIT_REFERENCE_PENDING' and r.get('blocking') is False]), 1)
                self.assertTrue(all(r['sku_ids'] == [payload['sku_id']] for r in result['input_requests']))
                self.assertIsNone(z.build_draft(result)['evidence']['packed_dimensions' if key == 'dimensions' else 'packed_weight'])

    def test_over_15kg_and_boundary(self):
        for text, blocked in (('15000 g', False), ('15 kg', False), ('15.001 kg', True)):
            self.payload['fields']['weight'] = text
            result = z.normalize(self.payload)
            self.assertEqual(result['unshippable'], blocked)
            if blocked:
                self.assertEqual(result['blocking_reasons'][0]['code'], 'ZYING_DETAIL_OVER_15KG')
                self.assertIn('不可发', result['input_requests'][0]['request'])
                self.assertEqual(result['status'], 'BLOCKED_EVIDENCE')

    def test_non_mlm_rejected(self):
        for sid in ('MLB1234567890', 'CBT1234567890', 'TEST_MLM1234567890', '../MLM1234567890'):
            self.payload['sku_id'] = sid
            with self.assertRaisesRegex(ValueError, 'MLM'):
                z.normalize(self.payload)

    def test_site_mismatch_rejected(self):
        self.payload['site_id'] = 'MLB'
        with self.assertRaises(ValueError):
            z.normalize(self.payload)

    def test_ambiguous_or_absent_units_not_estimated(self):
        for text in ('2.5', '约2.5kg', '-1 kg', 'NaN kg', '2-3 kg', '2,5 kg', '0 g'):
            self.payload['fields']['weight'] = text
            self.assertIsNone(z.normalize(self.payload)['package']['weight_g'])
        self.payload['fields']['dimensions'] = '32×20×18'
        self.assertIsNone(z.normalize(self.payload)['package']['length_cm'])

    def test_currency_not_guessed_and_duplicates_not_chosen(self):
        self.payload['fields']['price'] = '$1200'
        self.payload['fields']['net_proceeds'] = 'USD 20 USD 30'
        result = z.normalize(self.payload)
        self.assertEqual(result['buyer_price'], {'USD': None, 'MXN': None})
        self.assertIsNone(result['net_proceeds']['USD'])

    def test_full_and_unknown_optional_values_preserved(self):
        self.payload['fields'].update(fulfillment='FULL', sales='1万+', commission='未知')
        result = z.normalize(self.payload)
        self.assertEqual(result['fulfillment'], 'FULL')
        self.assertIsNone(result['sales'])
        self.assertIsNone(result['commission_pct'])
        self.assertEqual(result['raw']['sales'], '1万+')

    def test_no_supplier_1688_rule_relaxation(self):
        from src.package_recovery_v3 import extract
        from src.packaging_table_v3 import materialize
        self.assertEqual(extract('商品尺寸 32×20×18cm 商品重量 2.5kg'), {})
        capture = {'offer_id': '123456', 'skus': [{'sku_id': 's', 'package': {}}],
                   'package_rows': [{'sku_id': 's', 'dimension_type': 'PRODUCT', 'weight_type': 'NET_WEIGHT',
                                     'package': dict(zip(z.FIELDS, (32, 20, 18, 2500)))}]}
        result = materialize(capture)
        self.assertTrue(all(result['skus'][0]['package'][k] is None for k in z.FIELDS))
        from src.source_intake import normalize_collection
        with self.assertRaises(ValueError):
            normalize_collection(self.payload)

    def test_forbidden_field_and_wrong_host_rejected(self):
        for changes in ({'password': 'TEST_ONLY'}, {'source_url': 'https://example.invalid/detail'},
                        {'source_url': 'https://user@example.invalid/detail'}, {'captured_at': '2026-10-21'}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    z.normalize({**self.payload, **changes})

    def test_local_save_preserves_edits_and_creates_no_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('src.auto_processing_v3.enqueue', side_effect=AssertionError('must not enqueue')):
                first = z.save(root, self.payload)
                original = root / 'drafts' / first['name']
                original.write_text('{"edited":true}', encoding='utf-8')
                second = z.save(root, self.payload)
            self.assertNotEqual(first['name'], second['name'])
            self.assertEqual(original.read_text(encoding='utf-8'), '{"edited":true}')
            self.assertEqual(set(p.name for p in root.iterdir()), {'drafts','data'})
            self.assertFalse(first['enqueued'])

    def test_workbench_reads_packaging_and_blockers(self):
        from src.draft_editor import list_drafts
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.payload['fields']['weight'] = '16 kg'
            z.save(root, self.payload)
            rows = list_drafts(root / 'drafts', root)
            self.assertEqual(rows[0]['weight_g'], 16000)
            self.assertEqual(rows[0]['dimensions_cm'], [32, 20, 18])
            self.assertEqual(rows[0]['stage'], 'BLOCKED_EVIDENCE')
            self.assertIn('不可发', rows[0]['next_action'])
            self.assertEqual(rows[0]['input_requests'][0]['code'], 'ZYING_DETAIL_OVER_15KG')

    def test_receiver_route_offline_ephemeral_port(self):
        from src.collector_receiver import ensure_receiver
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server, _ = ensure_receiver(root, 0)
            opener = build_opener(ProxyHandler({}))
            url = f'http://127.0.0.1:{server.server_port}/api/market-reference'
            try:
                data = json.dumps({'operation': 'zying_detail', 'detail': self.payload}).encode()
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request(url, data=data), timeout=2)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                request = Request(url, data=data, headers={'X-Youyou-Collector': '1', 'Content-Type': 'application/json'})
                with opener.open(request, timeout=2) as response:
                    result = json.load(response)
                self.assertTrue(result['ok'])
                self.assertTrue((root / 'drafts' / result['name']).is_file())
                self.assertEqual(set(p.name for p in root.iterdir()), {'drafts','data'})
            finally:
                server.shutdown()
                server.server_close()

    def test_calculator_origin_is_kept_in_package_evidence(self):
        self.payload['field_origin'] = {'dimensions': 'zying_calculator_prefill', 'weight': 'zying_calculator_prefill'}
        detail = z.normalize(self.payload)
        draft = z.build_draft(detail)
        for key in ('packed_weight', 'packed_dimensions'):
            self.assertEqual(draft['evidence'][key]['field_origin'], 'zying_calculator_prefill')
        self.assertEqual(detail['package']['evidence']['field_origin']['dimensions'], 'zying_calculator_prefill')
        self.assertEqual(detail['package']['weight_g'], 2500)

    def test_reference_values_never_supply_package_or_price(self):
        self.payload['fields'].update(dimensions='', weight='', price='')
        self.payload['reference'] = {key: {'value': '550'} for key in ('purchase_cost_cny', 'shipping_cost_cny', 'converted_price_cny')}
        detail = z.normalize(self.payload)
        self.assertEqual(detail['reference']['shipping_cost_cny']['value'], '550')
        self.assertTrue(all(detail['package'][key] is None for key in z.FIELDS))
        self.assertTrue(all(v is None for v in detail['buyer_price'].values()))
        self.assertEqual(detail['status'], 'BLOCKED_EVIDENCE')

    def profit_payload(self, margin='-', profit='-', selling='407.62'):
        value = copy.deepcopy(self.payload)
        value['zying_profit_reference'] = {
            'inputs': {'purchase_cost_cny': {'raw': '10'}, 'shipping_cost_cny': {'raw': '58.57'},
                       'commission_pct': {'raw': '18.5%'}, 'selling_price_mxn': {'raw': selling}},
            'results': {'margin_pct': {'raw': margin}, 'net_profit': {'raw': profit}}}
        return value

    def test_profit_uncalculated_is_null_pending_and_nonblocking(self):
        for placeholder in ('', '-'):
            detail = z.normalize(self.profit_payload(placeholder, placeholder))
            ref = detail['zying_profit_reference']
            self.assertFalse(ref['trial_calculated'])
            self.assertEqual(ref['margin_hint'], 'PENDING')
            self.assertIsNone(ref['net_profit'][0]['value'])
            self.assertIsNone(ref['margin_pct']['value'])
            self.assertEqual(detail['status'], 'CAPTURE_READY')
            self.assertFalse(detail['blocking_reasons'])
            self.assertFalse(detail['input_requests'][0]['blocking'])

    def test_profit_margin_three_states_strict_floor_and_negative(self):
        for margin, hint in (('10%', 'BELOW_FLOOR'), ('10.01%', 'PASS'), ('0%', 'BELOW_FLOOR'), ('-2%', 'BELOW_FLOOR'), ('-', 'PENDING')):
            with self.subTest(margin=margin):
                ref = z.normalize(self.profit_payload(margin))['zying_profit_reference']
                self.assertEqual(ref['margin_hint'], hint)
                self.assertEqual(ref['basis_verified'], False)
                self.assertEqual(ref['margin_policy']['basis'], 'BUYER_PRICE')
                self.assertEqual(ref['margin_policy']['minimum_margin_operator'], 'GT')

    def test_profit_multicurrency_and_same_currency_ratio_only(self):
        ref = z.normalize(self.profit_payload(profit='MXN 61.143 / USD 3.2 / CNY 22'))['zying_profit_reference']
        self.assertEqual(ref['margin_hint'], 'PASS')
        self.assertAlmostEqual(ref['comparison_margin_pct'], 15)
        self.assertEqual([p['currency'] for p in ref['net_profit']], ['MXN', 'USD', 'CNY'])
        for item in ref['net_profit']:
            self.assertEqual(item['field_origin'], 'zying_calculator_result')
            self.assertTrue(item['captured_at'])
        self.assertEqual(ref['inputs']['shipping_cost_cny']['currency'], 'CNY')
        self.assertEqual(ref['inputs']['commission_pct']['unit'], '%')
        self.assertEqual(ref['inputs']['purchase_cost_cny']['field_origin'], 'zying_calculator_prefill')
        ref = z.normalize(self.profit_payload(profit='USD 22'))['zying_profit_reference']
        self.assertTrue(ref['trial_calculated'])
        self.assertEqual(ref['margin_hint'], 'PENDING')
        self.assertIsNone(ref['comparison_margin_pct'])

    def test_profit_uses_local_confirmed_policy_without_pricing_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            p = json.loads((z.ROOT / 'config/autopricing_policy.json').read_text(encoding='utf-8'))
            p['minimum_margin_pct'] = '20'
            (root / 'config/autopricing_policy.json').write_text(json.dumps(p), encoding='utf-8')
            detail = z.normalize(self.profit_payload('15%'), root)
            self.assertEqual(detail['zying_profit_reference']['margin_hint'], 'BELOW_FLOOR')
            draft = z.build_draft(detail)
            self.assertNotIn('pricing', draft['evidence'])
            self.assertFalse(draft['human_approved'])
            self.assertEqual(draft['payload']['pictures'], [])

    def test_main_image_workbench_count_and_unreviewed_gallery(self):
        from src.draft_editor import list_drafts
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = z.save(root, self.profit_payload('15%'))
            draft = json.loads((root / 'drafts' / saved['name']).read_text(encoding='utf-8'))
            row = list_drafts(root / 'drafts', root)[0]
            self.assertEqual(row['thumbnail'], self.payload['main_image'])
            self.assertEqual(row['image_count'], 1)
            self.assertEqual(row['thumbnail_mode'], 'REMOTE_URL_ONLY')
            self.assertTrue(row['zying_profit_reference']['trial_calculated'])
            self.assertEqual(draft['evidence']['image_review']['source_gallery'][0]['review_status'], 'UNREVIEWED')
            self.assertEqual(draft['evidence']['image_review']['gallery'], [])
            self.assertFalse(draft['evidence']['images_human_reviewed'])
            self.assertFalse((root / 'assets').exists())

    def test_receiver_size_limits_and_current_origin_without_network(self):
        from src.collector_receiver import make_collector_handler
        from src.draft_editor import YOUYOU_EXTENSION_ORIGIN
        from io import BytesIO
        self.assertEqual(YOUYOU_EXTENSION_ORIGIN, 'chrome-extension://gjjohgdhpecbgodfcdmdnjedajbjakge')
        handler_type = make_collector_handler(Path('.'))
        def receive(operation, size):
            body = json.dumps({'operation': operation, 'detail': self.payload}).encode()
            body += b' ' * (size - len(body))
            handler = object.__new__(handler_type)
            handler.path = '/api/market-reference'
            handler.headers = {'X-Youyou-Collector': '1', 'Origin': YOUYOU_EXTENSION_ORIGIN, 'Content-Length': str(len(body))}
            handler.rfile = BytesIO(body)
            handler._json = lambda status, value: (status, value)
            with patch('src.zying_detail_v3.save', return_value={'ok': True}), patch('src.market_context_v3.handle', return_value={'ok': True}):
                return handler.do_POST()
        for operation, size, status in (('zying_detail', 12001, 200), ('zying_detail', 32768, 200),
                                         ('zying_detail', 32769, 400), ('bind', 12000, 200), ('bind', 12001, 400)):
            self.assertEqual(receive(operation, size)[0], status, (operation, size))

    def test_same_sku_recapture_merges_and_preserves_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = z.save(root, self.payload)
            original = root / 'drafts' / first['name']
            contents = original.read_bytes()
            second = z.save(root, self.payload)
            self.assertEqual(first['name'], second['name'])
            self.assertEqual(second['recapture_mode'], 'MERGED_PRESERVE_EDITS')
            self.assertEqual(json.loads(original.read_bytes())['payload'],json.loads(contents)['payload'])
            self.assertEqual(len(list((root / 'drafts').glob('*.json'))), 1)
            self.assertEqual(len(list((root/'draft_versions').glob('*before-save*'))),1)


if __name__ == '__main__':
    unittest.main()
