"""HTML -> JS parser -> real collector HTTP -> persisted draft, fully local."""
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.collector_receiver import ensure_receiver
from src.draft_editor import YOUYOU_EXTENSION_ORIGIN, list_drafts

HERE = Path(__file__).resolve().parent
NODE = (os.environ.get('NODE_BINARY') or shutil.which('node') or
        str(Path(__import__('tests.node_runtime',fromlist=['node']).node())))
LIMIT = 32768


@unittest.skipUnless(Path(NODE).is_file(), 'Node runtime required for HTML parser integration')
class ZyingDetailHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(HERE / 'zying_http_payloads.cjs')],
                                capture_output=True, encoding='utf-8', check=True, timeout=10)
        cls.fixtures = json.loads(result.stdout)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zying-http-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.cleanup_root)
        # The isolated receiver must have its own confirmed policy; absent
        # policy correctly yields PENDING and cannot exercise the GT10 branch.
        (self.root / 'config').mkdir()
        shutil.copyfile(HERE.parent / 'config/autopricing_policy.json',
                        self.root / 'config/autopricing_policy.json')
        enqueue = patch('src.auto_processing_v3.enqueue', side_effect=AssertionError('must not enqueue'))
        enqueue.start()
        self.addCleanup(enqueue.stop)
        self.server, status = ensure_receiver(self.root, 0)
        self.assertEqual(status['state'], 'STARTED')
        self.assertNotIn(self.server.server_port, (8789, 8790, 18080, 18081))
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()

    def cleanup_root(self):
        self.temp.cleanup()
        self.assertFalse(self.root.exists())

    def post(self, detail=None, *, value=None, size=None, origin=YOUYOU_EXTENSION_ORIGIN, raw=None):
        if value is None:
            value = {'operation': 'zying_detail', 'detail': detail or self.fixtures['normal']}
        body = raw if raw is not None else json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if size is not None:
            self.assertLessEqual(len(body), size)
            body += b' ' * (size - len(body))
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('POST', '/api/market-reference', body=body,
                               headers={'Content-Type':'application/json', 'X-Youyou-Collector':'1', 'Origin':origin})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def draft(self, detail=None, **kwargs):
        status, result = self.post(detail, **kwargs)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['ok'])
        self.assertFalse(result['enqueued'])
        self.assertFalse(result['publishing_performed'])
        path = self.root / 'drafts' / result['name']
        self.assertTrue(path.is_file())
        self.assertTrue({'config','drafts','data'} <= {p.name for p in self.root.iterdir()})
        self.assertTrue((self.root / result['payload_archive']).is_file())
        self.assertFalse((self.root/'data/v3/auto-processing').exists())
        return json.loads(path.read_text(encoding='utf-8')), result

    def test_html_payload_persists_provenance_image_and_pending_profit(self):
        draft, result = self.draft()
        evidence = draft['evidence']
        detail = evidence['zying_detail']
        self.assertEqual(draft['payload']['title'], self.fixtures['normal']['fields']['title'])
        self.assertEqual(result['status'], 'CAPTURE_READY')
        self.assertEqual([detail['package'][k] for k in ('length_cm','width_cm','height_cm','weight_g')], [14,13,12,550])
        for key in ('packed_dimensions', 'packed_weight'):
            self.assertEqual(evidence[key]['source'], 'zying_detail')
            self.assertEqual(evidence[key]['field_origin'], 'zying_calculator_prefill')
            self.assertEqual(evidence[key]['source_url'], self.fixtures['normal']['source_url'])
        self.assertEqual(detail['package']['evidence']['field_origin']['weight'], 'zying_calculator_prefill')
        row = list_drafts(self.root / 'drafts', self.root)[0]
        self.assertEqual(row['image_count'], 1)
        self.assertEqual(row['thumbnail'], self.fixtures['normal']['main_image'])
        self.assertEqual(row['thumbnail_mode'], 'REMOTE_URL_ONLY')
        self.assertEqual(evidence['image_review']['source_gallery'][0]['review_status'], 'UNREVIEWED')
        self.assertEqual(draft['payload']['pictures'], [])
        self.assertFalse(evidence['images_human_reviewed'])
        self.assertEqual(evidence['zying_profit_reference']['margin_hint'], 'PENDING')
        request = next(r for r in draft['input_requests'] if r['code']=='ZYING_PROFIT_REFERENCE_PENDING')
        self.assertFalse(request['blocking'])

    def test_maximum_reasonable_payload_and_all_profit_fields(self):
        draft, _ = self.draft(self.fixtures['maximum'])
        self.assertEqual(len(draft['payload']['title']), 300)
        detail = draft['evidence']['zying_detail']
        self.assertEqual(len(detail['main_image']), 1000)
        self.assertGreater(len(detail['source_url']), 900)
        ref = draft['evidence']['zying_profit_reference']
        self.assertEqual(set(ref['inputs']), {'purchase_cost_cny','shipping_cost_cny','converted_price_cny','commission_pct','selling_price_mxn'})
        self.assertTrue(all(item['value'] is not None for item in ref['inputs'].values()))
        self.assertEqual({p['currency'] for p in ref['net_profit']}, {'MXN','USD','CNY'})
        self.assertEqual(ref['margin_hint'], 'PASS')
        self.assertTrue(ref['reference_only'])
        self.assertFalse(ref['basis_verified'])
        self.assertNotIn('pricing', draft['evidence'])

    def test_profit_three_states_strict_floor_over_http(self):
        for margin, expected in (('-', 'PENDING'), ('10%', 'BELOW_FLOOR'), ('10.01%', 'PASS')):
            with self.subTest(margin=margin):
                payload = copy.deepcopy(self.fixtures['maximum'])
                payload['zying_profit_reference']['results'] = {'margin_pct':{'raw':margin}, 'net_profit':{'raw':'-'}}
                draft, _ = self.draft(payload)
                self.assertEqual(draft['evidence']['zying_profit_reference']['margin_hint'], expected)

    def test_missing_package_fields_are_input_requests_over_http(self):
        payload = copy.deepcopy(self.fixtures['normal'])
        payload['fields'].update(dimensions='', weight='')
        draft, result = self.draft(payload)
        self.assertEqual(result['status'], 'BLOCKED_EVIDENCE')
        missing = {r['field'] for r in draft['input_requests'] if r.get('blocking') is not False and r.get('origin')!='CONTRIBUTION_INPUT'}
        self.assertEqual(missing, {'length_cm','width_cm','height_cm','weight_g'})
        self.assertIsNone(draft['evidence']['packed_dimensions'])
        self.assertIsNone(draft['evidence']['packed_weight'])

    def test_15kg_boundary_and_overweight_over_http(self):
        for weight, blocked in (('15000 g', False), ('15001 g', True)):
            payload = copy.deepcopy(self.fixtures['normal'])
            payload['fields']['weight'] = weight
            draft, result = self.draft(payload)
            self.assertEqual(result['unshippable'], blocked)
            if blocked:
                self.assertEqual(result['status'], 'BLOCKED_EVIDENCE')
                self.assertIn('ZYING_DETAIL_OVER_15KG', [r['code'] for r in draft['input_requests']])

    def test_non_mlm_rejected_without_draft_over_http(self):
        for changes in ({'site_id':'MLB'}, {'sku_id':'MLB6281697608'}, {'sku_id':'CBT6281697608'}):
            status, result = self.post({**self.fixtures['normal'], **changes})
            self.assertEqual(status, 400)
            self.assertIn('MLM', result['error'])
            self.assertFalse((self.root / 'drafts').exists())

    def test_actual_http_body_limits_and_structural_envelope(self):
        def length(detail):
            return len(json.dumps({'operation':'zying_detail','detail':detail}, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        self.assertGreater(length(self.fixtures['envelope']), 12000)
        self.assertLess(length(self.fixtures['envelope']), LIMIT)
        self.assertLess(length(self.fixtures['maximum']), LIMIT)
        for size in (12001, LIMIT):
            self.draft(self.fixtures['maximum'], size=size)
        count = len(list((self.root / 'drafts').glob('*.json')))
        for size in (LIMIT+1, 65536):
            status, result = self.post(size=size)
            self.assertEqual(status, 400)
            self.assertFalse(result['ok'])
        self.assertEqual(len(list((self.root / 'drafts').glob('*.json'))), count)
        # Resolve is the real other-operation handler, with no save/handle mocks.
        for size, expected in ((12000,200),(12001,400),(LIMIT,400)):
            status, _ = self.post(value={'operation':'resolve','tab_id':'123'}, size=size)
            self.assertEqual(status, expected)
        self.assertEqual({p.name for p in (self.root/'data/v3').iterdir()}, {'zying_detail_payloads'})

    def test_origin_malformed_json_and_non_object_rejected_over_http(self):
        self.assertEqual(self.post(origin='https://example.invalid')[0], 403)
        for raw in (b'{', b'[]', b'', b'null'):
            self.assertEqual(self.post(raw=raw)[0], 400)
        self.assertFalse((self.root / 'drafts').exists())


if __name__ == '__main__':
    unittest.main()
