import copy
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import auto_processing_v3 as queue, sku_processing_v3 as pipeline
from src.source_image_screening_v3 import screen
from src.context_segments_v3 import segments, bound_facts
from src.publish_service import build_user_product_payload
from src.supplier_scope_v3 import attributes_for_sku


def fixture():
    return {'payload': {'title': 'Supplier product', 'family_name': 'Product', 'description': '',
        'category_id': 'CBT1', 'sites_to_sell': [{'site_id': 'MLM', 'net_proceeds': 5}],
        'attributes': [{'id': 'CAPACITY', 'value_name': 'wrong-common'}]},
        'evidence': {'sku_details': [{'sku_id': 'a', 'variant': '250ml', 'seller_sku': 'A', 'available_quantity': 200},
            {'sku_id': 'b', 'variant': '500ml', 'seller_sku': 'B', 'available_quantity': 200}],
            'supplier_page': {'title': 'Supplier product', 'description': 'Real source', 'skus': [
                {'sku_id': 'a', 'variant': '250ml'}, {'sku_id': 'b', 'variant': '500ml'}]},
            'image_review': {'sku_source_galleries': [{'sku_id': 'a', 'gallery': [{'remote_url': 'https://a/ok'}, {'remote_url': 'https://a/duplicate'}, {'remote_url': 'https://a/watermark'}]},
                {'sku_id': 'b', 'gallery': [{'remote_url': 'https://b/ok'}]}]}}}


class AutomationTests(unittest.TestCase):
    def test_duplicate_collection_preserves_completed_gallery_and_copy(self):
        from src.source_intake import save_collection_and_enqueue
        from tests.test_source_intake import SourceIntakeTests
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sample=SourceIntakeTests();sample.setUp()
            result=save_collection_and_enqueue(sample.payload,root);path=result['draft']
            draft=json.loads(path.read_text(encoding='utf-8'));draft['payload']['description']='completed copy'
            draft['evidence']['processing_automation']={'version':pipeline.VERSION}
            draft['evidence']['image_review']['gallery']=[{'local_file':'screened-source/selected.jpg','auto_screened':True}]
            queue.write(path,draft)
            second=save_collection_and_enqueue(sample.payload,root)
            self.assertTrue(second['auto_processing']['duplicate'])
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),draft)

    def test_intake_retains_long_source_and_does_not_copy_other_sku_cost_or_package(self):
        from src.source_intake import normalize_collection, build_processing_draft
        from tests.test_source_intake import SourceIntakeTests
        sample=SourceIntakeTests();sample.setUp();raw=sample.payload
        raw['description']='完整资料'*4000+'最后一个关键事实'
        raw['skus'].append({'sku_id':'sku-2','variant':'另一款'})
        normalized=normalize_collection(raw)
        self.assertEqual(normalized['description'],raw['description'])
        root=Path('test');draft=build_processing_draft(normalized,root/'intake.json',root)
        second=draft['evidence']['sku_details'][1]
        self.assertIsNone(second['purchase_cost_cny']);self.assertIsNone(second['package']['weight_g'])

    def test_manual_switch_does_not_copy_common_content_and_retains_each_sku_edit(self):
        from src.draft_editor import create_edited_version
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);draft=fixture();draft['payload']['description']='common-A'
            draft['evidence']['current_pricing_sku_id']='a'
            for sku in draft['evidence']['sku_details']:
                sku['ai_content']={'description':sku['variant'],'attributes':[],'category_id':'CBT1'}
            queue.write(root/'drafts/test-draft.json',draft)
            changes={'current_pricing_sku_id':'b','payload':copy.deepcopy(draft['payload'])}
            path=create_edited_version('test-draft.json',changes,root/'drafts',root/'draft_versions')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['evidence']['sku_details'][1]['ai_content']['description'],'500ml')
            changes['sku_content_edits']={'a':{'description':'A edited'},'b':{'description':'B edited'}}
            path=create_edited_version('test-draft.json',changes,root/'drafts',root/'draft_versions')
            self.assertEqual([s['ai_content']['description'] for s in json.loads(path.read_text(encoding='utf-8'))['evidence']['sku_details']],['A edited','B edited'])

    def test_package_verification_is_not_shared_with_other_sku(self):
        draft=fixture();draft['evidence']['current_pricing_sku_id']='a'
        draft['evidence']['packed_weight']={'value':200,'unit':'g','status':'USER_CONFIRMED'}
        draft['evidence']['sku_details'][1]['package']={'weight_g':400}
        result=pipeline.scope(draft,draft['evidence']['sku_details'][1])
        self.assertEqual(result['evidence']['packed_weight']['status'],'UNVERIFIED')
        self.assertNotIn('product_weight_g',result['evidence']['supplier_page'])

    def test_real_collector_receipt_automatically_dispatches_without_button(self):
        from src.collector_receiver import ensure_receiver
        from src.draft_editor import YOUYOU_EXTENSION_ORIGIN
        from urllib.request import Request, build_opener, ProxyHandler
        from tests.test_source_intake import SourceIntakeTests
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); event=threading.Event(); stopped=threading.Event(); received=[]
            def start(name, drafts, workspace):
                received.append(name); event.set(); return {'status':'queued'}
            worker=queue.start_worker(root, types.SimpleNamespace(start_full_ai_flow=start), stopped)
            server,_=ensure_receiver(root,0)
            try:
                data=SourceIntakeTests();data.setUp()
                request=Request(f'http://127.0.0.1:{server.server_port}/api/collect', data=json.dumps(data.payload).encode(),
                    headers={'Content-Type':'application/json','Origin':YOUYOU_EXTENSION_ORIGIN,'X-Youyou-Collector':'1'})
                with build_opener(ProxyHandler({})).open(request,timeout=3) as response: result=json.load(response)
                self.assertTrue(result['ok']);self.assertEqual(result['auto_processing']['status'],'pending')
                self.assertTrue(event.wait(3),'collection never reached the automatic worker')
                self.assertEqual(received,['1688-856658055965-draft.json'])
            finally:
                stopped.set();queue.WAKE.set();worker.join(2);server.shutdown();server.server_close()

    def test_target_store_published_records_are_not_automatically_reprocessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'drafts/test-draft.json'
            queue.write(root/'data/v3/publishing/records/store2/test-draft.json', {'status':'pending_confirmation'})
            self.assertEqual(queue.enqueue(root,path,{'title':'updated'})['status'],'SKIPPED_PUBLISHED')

    def test_supplier_table_headers_and_other_sku_are_not_evidence(self):
        draft = fixture(); skus = draft['evidence']['sku_details']
        attrs = {'表面处理': '全部630650 全部 630 650', '规格型号': '重量(g)',
            '500ml': 'other variant table', '250ml': 'selected price inventory row', '材质': '钢'}
        self.assertEqual(attributes_for_sku(attrs, skus[0], skus), {'材质': '钢'})

    def test_manual_save_keeps_all_sku_outputs_and_updates_selected_only(self):
        from src.draft_editor import create_edited_version
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); draft = fixture()
            draft['evidence']['current_pricing_sku_id'] = 'a'
            for sku in draft['evidence']['sku_details']:
                sku['ai_content'] = {'description': sku['variant'], 'attributes': [], 'category_id': 'CBT1'}
                sku['ai_processing'] = {'status': 'done'}
            queue.write(root/'drafts/test-draft.json', draft)
            path = create_edited_version('test-draft.json', {'payload': {'description': 'Human correction', 'attributes': []}}, root/'drafts', root/'draft_versions')
            saved = json.loads(path.read_text(encoding='utf-8'))
            rows = saved['evidence']['sku_details']
            self.assertEqual(rows[0]['ai_content']['description'], 'Human correction')
            self.assertEqual(rows[1]['ai_content']['description'], '500ml')
            self.assertEqual(rows[1]['ai_processing']['status'], 'done')

    def test_collection_queue_dedup_published_and_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'drafts' / 'test-draft.json'
            queue.write(path, fixture())
            self.assertEqual(queue.enqueue(root, path, {'title': 'new', 'captured_at': '1'})['status'], 'pending')
            self.assertTrue(queue.enqueue(root, path, {'title': 'new', 'captured_at': '2'})['duplicate'])
            legacy = types.SimpleNamespace(start_full_ai_flow=Mock(return_value={'status': 'queued'}))
            stopped = threading.Event()
            queue.dispatch_once(root, legacy, stopped)
            legacy.start_full_ai_flow.assert_called_once_with(path.name, root / 'drafts', root)
            queue.write(root / 'data' / 'publish_records' / (path.stem + '.json'), {'status': 'success'})
            self.assertEqual(queue.enqueue(root, path, {'title': 'changed'})['status'], 'SKIPPED_PUBLISHED')

    def test_scope_removes_other_variant_and_common_attributes(self):
        draft = fixture(); result = pipeline.scope(draft, draft['evidence']['sku_details'][0])
        self.assertEqual([s['sku_id'] for s in result['evidence']['sku_details']], ['a'])
        self.assertNotIn('500ml', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['payload']['attributes'], [])
        self.assertEqual(draft['payload']['attributes'][0]['value_name'], 'wrong-common')

    def test_screen_deduplicates_bytes_and_rejects_watermark_per_sku(self):
        draft = fixture()
        def inspect(root, draft, sku, image):
            watermark = image['remote_url'].endswith('watermark')
            return {'usable': True, 'contains_watermark': watermark, 'wrong_variant': False,
                'uncertain': False, 'fingerprint': sku['sku_id'], 'reason': 'test'}
        reports = screen(Path('.'), draft, draft['evidence']['sku_details'], inspector=inspect)
        self.assertEqual([r['usable_count'] for r in reports], [1, 1])
        groups = draft['evidence']['image_review']['sku_listing_galleries']
        self.assertTrue(all('watermark' not in i['remote_url'] for g in groups for i in g['gallery']))
        self.assertEqual(reports[0]['missing_count'], 2)
        self.assertFalse(draft['evidence']['image_generation_review']['automatically_generated'])

    def test_screen_never_overwrites_human_locked_selection(self):
        draft = fixture(); draft['edit_metadata'] = {'field_locks': {'pictures': True}}
        draft['evidence']['image_review']['sku_listing_galleries'] = [{'sku_id': 'a', 'gallery': [{'local_file': 'chosen.jpg'}]}]
        screen(Path('.'), draft, draft['evidence']['sku_details'], inspector=lambda *args: {'usable': True})
        self.assertEqual(draft['evidence']['image_review']['sku_listing_galleries'][0]['gallery'], [{'local_file': 'chosen.jpg'}])

    def test_every_source_segment_is_visited_and_invented_quote_dropped(self):
        facts = {'supplier_description': 'abcdefghij' * 1100}
        chunks = segments(facts); calls = []
        self.assertEqual(''.join(r['text'] for group in chunks for r in group), facts['supplier_description'])
        def call(config, request):
            calls.append(request); leaf = json.loads(request['input'])[0]
            return {'output': [{'content': [{'text': json.dumps({'quotes': [{'path': leaf['path'], 'quote': 'abcde'}, {'path': leaf['path'], 'quote': 'invented-fact'}]})}]}]}
        compact, report = bound_facts(facts, {}, 'local', call)
        self.assertEqual(len(calls), len(chunks))
        self.assertTrue(all(q['quote'] != 'invented-fact' for q in compact['source_quotes']))
        self.assertEqual(report['segments'], len(chunks))

    def test_all_skus_run_after_one_failure_and_no_paid_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'drafts' / 'test-draft.json'
            queue.write(path, fixture()); full = root / 'jobs' / 'test-draft-full-ai-job.json'
            queue.write(full, {'status': 'queued'})
            calls = []
            def apply(path, root, full, job):
                draft = json.loads(path.read_text(encoding='utf-8')); sid = draft['evidence']['sku_details'][0]['sku_id']; calls.append(sid)
                if sid == 'a': raise ValueError('missing fact a')
                draft['payload'].update(title='500ml product', description='Only SKU b', attributes=[{'id': 'CAPACITY', 'value_name': '500ml'}])
                queue.write(path, draft)
            legacy = types.SimpleNamespace(ROOT=root, DRAFTS_DIR=root/'drafts', EditorError=ValueError,
                query_official_category=lambda category,root:{'category_id':category,'name':'Test category','query_source':'MERCADOLIBRE_OFFICIAL_API_LIVE'},
                discover_official_category=Mock(side_effect=AssertionError('unrelated category rediscovery')),
                get_public_settings=lambda root:{},
                read_json=lambda p: json.loads(p.read_text(encoding='utf-8')),
                _draft_path=lambda n,d: d/n, _apply_ai_content_and_attributes=apply,
                refresh_competitor_pricing=Mock(side_effect=ValueError('missing cost')),
                publish_preflight_issues=lambda d: [],
                start_image_generation=Mock(side_effect=AssertionError('paid image called')),
                start_sku_image_generation=Mock(side_effect=AssertionError('paid sku image called')))
            def save(p, job, **updates): job.update(updates); queue.write(p, job)
            legacy._save_full_job = save
            pipeline.install(legacy)
            with patch('src.source_image_screening_v3.screen', return_value=[]):
                # Screen mock must still set review metadata as a real screen does.
                def screen_mock(root, draft, skus, **kw):
                    draft['evidence']['image_generation_review'] = {'automatically_generated': False}
                    return []
                with patch('src.source_image_screening_v3.screen', side_effect=screen_mock): legacy._run_full_ai_flow(path, root, full)
            result = legacy.read_json(path); job = legacy.read_json(full)
            self.assertEqual(calls, ['a', 'b']); self.assertEqual(job['status'], 'done')
            self.assertEqual(result['evidence']['sku_details'][0]['ai_processing']['status'], 'failed')
            self.assertEqual(result['evidence']['sku_details'][1]['ai_content']['description'], 'Only SKU b')
            self.assertTrue(any('SKU A' in i for i in legacy.publish_preflight_issues(result)))
            legacy.start_image_generation.assert_not_called(); legacy.start_sku_image_generation.assert_not_called()
            self.assertTrue(result['evidence']['processing_automation']['profit_requires_human_confirmation'])

    def test_publish_uses_separate_descriptions_and_attributes(self):
        draft = fixture()
        for sku in draft['evidence']['sku_details']:
            sku['ai_content'] = {'description': sku['variant'], 'attributes': [{'id': 'CAPACITY', 'value_name': sku['variant']}]}
        items = build_user_product_payload(draft, {'a': ['1'], 'b': ['2']})
        self.assertEqual([i['description']['plain_text'] for i in items], ['250ml', '500ml'])
        self.assertEqual([next(a['value_name'] for a in i['attributes'] if a['id']=='CAPACITY') for i in items], ['250ml', '500ml'])


if __name__ == '__main__': unittest.main()
