import copy
import csv
import io
import json
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from src import input_requests_v3 as mod, draft_editor, auto_processing_v3


class InputRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'drafts').mkdir()
        self.name = 'a-draft.json'
        self.draft = {'payload': {'title': '测试', 'site_id': 'MLM'}, 'human_approved': True,
                      'evidence': {'sku_details': [{'sku_id': 's1', 'package': {'length_cm': 20},
                          'supplier_facts': {'untouched': '原始'}, 'custom': '保留',
                          'site_pricing': [{'site_id': 'MLM'}]}], 'images_human_reviewed': True},
                      'input_requests': [{'code': 'COST_MISSING', 'field': 'purchase_cost_cny',
                                          'sku_ids': ['s1'], 'request': '缺采购成本'}]}
        self.write()

    def write(self, name=None, value=None):
        (self.root / 'drafts' / (name or self.name)).write_text(
            json.dumps(value if value is not None else self.draft, ensure_ascii=False), encoding='utf-8')

    def row(self, field='purchase_cost_cny', value='12 CNY'):
        row = next(r for r in mod.aggregate(self.root)['rows'] if r['field'] == field)
        return {**row, 'fill_value': value}

    def add(self, field, sid='s1', **extra):
        self.draft['input_requests'].append({'field': field, 'sku_ids': [sid], 'request': field, **extra})
        self.write()

    def preview(self, rows):
        return mod.import_table(self.root, {'rows': rows})

    def confirm(self, rows, preview=None, legacy=None):
        p = preview if preview is not None else self.preview(rows)
        return mod.import_table(self.root, {'rows': rows, 'mode': 'confirm', 'confirm': True,
                                           'preview_hash': p['preview_hash']}, legacy)

    def test_stable_ids_scope_and_read_only(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        a = mod.aggregate(self.root)
        self.assertEqual(a, mod.aggregate(self.root))
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.write('b-draft.json')
        rows = mod.aggregate(self.root)['rows']
        self.assertEqual(2, len(rows))
        self.assertEqual(a['rows'][0]['request_id'], rows[0]['request_id'])
        self.assertNotEqual(rows[0]['request_id'], rows[1]['request_id'])

    def test_draft_queue_job_and_blocked_sources(self):
        (self.root / 'jobs').mkdir()
        (self.root / 'jobs/a-draft-full-ai-job.json').write_text(json.dumps({'items': [
            {'sku_id': 's1', 'state': 'BLOCKED_EVIDENCE', 'issues': [
                {'field': 'weight_g', 'reason': '缺重量'}]}]}), encoding='utf-8')
        q = self.root / 'data/v3/auto-processing'; q.mkdir(parents=True)
        (q / 'a-draft.json').write_text(json.dumps({'input_requests': self.draft['input_requests']}), encoding='utf-8')
        rows = mod.aggregate(self.root)['rows']
        self.assertEqual(2, len(rows))
        self.assertIn('auto-processing', rows[0]['source'])
        self.assertIn('jobs/', rows[1]['source'])

    def test_csv_bom_roundtrip_and_formula_escape(self):
        self.draft['payload']['title'] = '=HYPERLINK("bad")'; self.write()
        data = mod.export_csv(self.root)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertTrue(rows[0]['product'].startswith("'="))
        rows[0]['fill_value'] = '9 CNY'
        out = io.StringIO(); w = csv.DictWriter(out, fieldnames=mod.COLUMNS); w.writeheader(); w.writerows(rows)
        self.assertEqual(9, mod.import_table(self.root, {'content': '\ufeff' + out.getvalue()})['updates'][0]['value'])

    def test_preview_does_not_write(self):
        before = (self.root / 'drafts' / self.name).read_bytes()
        p = self.preview([self.row()])
        self.assertTrue(p['valid'])
        self.assertEqual(before, (self.root / 'drafts' / self.name).read_bytes())
        self.assertFalse((self.root / 'draft_versions').exists())

    def test_confirm_scoped_save_versions_preserve_evidence(self):
        result = self.confirm([self.row()])
        self.assertEqual(1, len(result['written']))
        updated = mod.load(self.root / 'drafts' / self.name)
        expected = copy.deepcopy(self.draft['evidence']); expected['sku_details'][0]['purchase_cost_cny'] = 12
        self.assertEqual(expected, updated['evidence'])
        self.assertFalse(updated['human_approved'])
        self.assertEqual(2, len(list((self.root / 'draft_versions').glob('*.json'))))
        original = next((self.root / 'draft_versions').glob('*before-save*'))
        self.assertEqual(self.draft, mod.load(original))

    def test_confirm_needs_real_preview_and_explicit_confirmation(self):
        row = self.row()
        with self.assertRaises(ValueError):
            mod.import_table(self.root, {'rows': [row], 'mode': 'confirm', 'confirm': True})
        p = self.preview([row])
        with self.assertRaises(ValueError):
            mod.import_table(self.root, {'rows': [row], 'mode': 'confirm', 'preview_hash': p['preview_hash']})

    def test_stale_preview_and_changed_file_rejected(self):
        rows = [self.row()]; p = self.preview(rows)
        self.draft['payload']['title'] = '变化'; self.write()
        with self.assertRaises(ValueError):
            self.confirm(rows, p)

    def test_changed_fill_value_rejected(self):
        rows = [self.row()]; p = self.preview(rows)
        rows[0]['fill_value'] = 55
        with self.assertRaises(ValueError):
            self.confirm(rows, p)

    def test_unknown_and_tampered_request(self):
        self.assertFalse(self.preview([{'request_id': '../bad', 'fill_value': 1}])['valid'])
        for key in ('draft', 'sku_id', 'field'):
            row = self.row(); row[key] = 'bad'
            self.assertFalse(self.preview([row])['valid'])

    def test_duplicates_and_same_field_collision(self):
        row = self.row()
        self.assertFalse(self.preview([row, row])['valid'])
        self.add('purchase_cost_cny', code='OTHER_REASON')
        rows = [{**r, 'fill_value': 2} for r in mod.aggregate(self.root)['rows']]
        self.assertFalse(self.preview(rows)['valid'])

    def test_empty_values_skipped_and_zero_shipping_accepted(self):
        self.assertEqual(0, self.preview([{**self.row(), 'fill_value': ''}])['count'])
        self.add('domestic_shipping_cny')
        self.assertTrue(self.preview([self.row('domestic_shipping_cny', 0)])['valid'])

    def test_units_and_weight_boundary(self):
        self.add('weight_g')
        self.assertEqual(15000, self.preview([self.row('weight_g', '15 kg')])['updates'][0]['value'])
        self.assertFalse(self.preview([self.row('weight_g', '15.001 kg')])['valid'])
        self.assertFalse(self.preview([self.row('weight_g', '5 lb')])['valid'])
        self.add('length_cm')
        self.assertEqual(2, self.preview([self.row('length_cm', '20 mm')])['updates'][0]['value'])
        self.assertEqual(200, mod.validate_value('length_cm', '2 m'))

    def test_package_and_market_review_invalidated(self):
        sku = self.draft['evidence']['sku_details'][0]
        sku['package'].update(status='USER_CONFIRMED', field_status={'weight_g': 'MEASURED_CONFIRMED'},
                              field_sources={'weight_g': [{'quote': 'old measurement'}]})
        sku['market_reference'] = {'spec_match': 'CONFIRMED_SAME_SKU'}
        self.add('weight_g'); self.add('market_item_id')
        self.confirm([self.row('weight_g', '250 g'), self.row('market_item_id', 'MLM123456')])
        updated = mod.load(self.root/'drafts'/self.name)['evidence']['sku_details'][0]
        self.assertEqual('USER_INPUT_UNVERIFIED', updated['package']['field_status']['weight_g'])
        self.assertEqual(20, updated['package']['length_cm'])
        self.assertEqual('HUMAN_INPUT_TABLE', updated['package']['field_sources']['weight_g'][0]['source'])
        self.assertEqual('SOURCE_SELECTED_PENDING_REVIEW', updated['market_reference']['spec_match'])

    def test_running_task_blocks_import(self):
        jobs = self.root/'jobs'; jobs.mkdir()
        (jobs/'a-draft-full-ai-job.json').write_text('{"status":"running"}', encoding='utf-8')
        self.assertFalse(self.preview([self.row()])['valid'])

    def test_payload_sites_must_be_mlm(self):
        self.draft['payload']['sites_to_sell'] = [{'site_id': 'MLB'}]; self.write()
        self.assertFalse(self.preview([self.row()])['valid'])

    def test_numeric_invalid_and_range(self):
        for raw in ('NaN', 'Infinity', -1, 0, True, '4 USD', '1e9999', '1000001 CNY'):
            self.assertFalse(self.preview([self.row(value=raw)])['valid'], raw)
        with self.assertRaises(ValueError): mod.validate_value('length_cm', 1001)
        with self.assertRaises(ValueError): mod.validate_value('units_per_pack', 1.2)

    def test_only_mlm(self):
        self.add('market_item_id')
        self.assertTrue(self.preview([self.row('market_item_id', 'MLM123456')])['valid'])
        self.assertFalse(self.preview([self.row('market_item_id', 'MLB123456')])['valid'])
        self.draft['evidence']['sku_details'][0]['site_pricing'][0]['site_id'] = 'MLB'; self.write()
        self.assertFalse(self.preview([self.row()])['valid'])

    def test_unknown_fields_and_approval_never_writable(self):
        for field in ('human_approved', 'spec_match', 'sku_images', 'category_id', '__proto__'):
            self.add(field)
            self.assertFalse(self.preview([self.row(field, 'true')])['valid'])

    def test_captured_evidence_not_fabricated(self):
        self.draft['evidence']['zying_detail'] = {'sku_id': 's1', 'blocking_reasons': [
            {'field': 'weight_g', 'reason': '需重新采集'}]}; self.write()
        self.assertFalse(self.row('weight_g')['editable'])
        self.assertFalse(self.preview([self.row('weight_g', 200)])['valid'])

    def test_all_or_no_validation(self):
        rows = [self.row(), {'request_id': 'bad', 'fill_value': 1}]
        result = self.preview(rows)
        self.assertFalse(result['valid'])
        self.assertFalse((self.root / 'draft_versions').exists())

    def test_json_import(self):
        p = mod.import_table(self.root, {'content': '\ufeff' + json.dumps({'rows': [self.row()]}), 'format': 'json'})
        self.assertTrue(p['valid'])

    def test_malformed_record_reported_not_hidden(self):
        (self.root / 'drafts/b-draft.json').write_text('{bad', encoding='utf-8')
        out = mod.aggregate(self.root)
        self.assertEqual(1, out['count'])
        self.assertEqual('b-draft.json', out['warnings'][0]['file'])

    def test_resume_existing_queue_and_respect_pause(self):
        legacy = types.SimpleNamespace(save_edited_draft=draft_editor.save_edited_draft,
            DRAFTS_DIR=self.root/'drafts', VERSIONS_DIR=self.root/'draft_versions', read_json=mod.load)
        auto_processing_v3.install_edit_resume(legacy)
        q = self.root/'data/v3/auto-processing'; q.mkdir(parents=True)
        (q/'a-draft.json').write_text('{"status":"needs_attention"}', encoding='utf-8')
        with patch.object(auto_processing_v3, 'enqueue') as enqueue:
            self.confirm([self.row()], legacy=legacy)
            enqueue.assert_called_once()
        jobs=self.root/'jobs'; jobs.mkdir()
        (jobs/'a-draft-full-ai-job.json').write_text('{"status":"paused","pause_reason":"USER"}', encoding='utf-8')
        with patch.object(auto_processing_v3, 'enqueue') as enqueue:
            self.confirm([self.row(value=13)], legacy=legacy)
            enqueue.assert_not_called()

    def test_no_new_queue_without_existing_entry(self):
        legacy = types.SimpleNamespace(save_edited_draft=draft_editor.save_edited_draft,
            DRAFTS_DIR=self.root/'drafts', VERSIONS_DIR=self.root/'draft_versions', read_json=mod.load)
        auto_processing_v3.install_edit_resume(legacy)
        with patch.object(auto_processing_v3, 'enqueue') as enqueue:
            self.confirm([self.row()], legacy=legacy)
            enqueue.assert_not_called()

    def test_http_routes_temporary_server(self):
        from src.workbench_v3 import make_handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        with urlopen(url + '/api/v3/input-requests') as r:
            self.assertEqual(1, json.load(r)['count'])
        with urlopen(url + '/api/v3/input-requests/export.csv') as r:
            self.assertTrue(r.read().startswith(b'\xef\xbb\xbf'))
        def post(body):
            req = Request(url + '/api/v3/input-requests/import', data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
            with urlopen(req) as response: return json.load(response)
        rows = [self.row()]; p = post({'rows': rows, 'mode': 'dry_run'})
        with patch('src.workbench_v3.publishing.commit', side_effect=AssertionError('never publish')):
            result = post({'rows': rows, 'mode': 'confirm', 'confirm': True, 'preview_hash': p['preview_hash']})
        self.assertEqual(1, len(result['written']))


if __name__ == '__main__':
    unittest.main()
