import ast,copy,json,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from src import source_image_screening_v3 as screening
from src import source_image_policy_v3 as rules
from src.auto_processing_v3 import input_requests,write

class OriginalsOnly(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def config(self,**kw):
        p=self.root/'config/source_image_policy.json';p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(kw),encoding='utf-8')
    def draft(self,n=3,**kw):
        images=[{'remote_url':f'https://example.invalid/{i}.jpg',**kw} for i in range(n)]
        return {'payload':{'sites_to_sell':[{'site_id':'MLM'}]},'evidence':{'sku_details':[{'sku_id':'a'}],
            'image_review':{'source_gallery':images}}}
    def screen(self,d,verdict=None):
        inspect=Mock(side_effect=lambda r,d,s,i:dict(verdict or {'usable':True,'contains_watermark':False,'wrong_variant':False,'uncertain':False},fingerprint=i['remote_url']))
        with patch('src.source_image_resolution_v3.recover_image',side_effect=lambda r,i:(i,{})),patch('src.image_generation.run_job',side_effect=AssertionError('generation forbidden')) as generate:
            result=screening.screen(self.root,d,d['evidence']['sku_details'],inspector=inspect)
        generate.assert_not_called();return result,inspect
    def test_defaults(self):
        self.assertEqual(rules.policy(self.root)['image_generation_budget'],0)
        self.assertEqual(rules.MIN_IMAGES_PER_SKU,3)
    def test_bad_caps_fail_closed(self):
        for cap in (0,2,21,True,'8',3.5):
            with self.subTest(cap=cap):
                self.config(max_images_per_sku=cap)
                with self.assertRaises(ValueError):rules.policy(self.root)
    def test_positive_negative_nonfinite_budget_forbidden(self):
        for budget in (1,-1,True,'0',float('nan'),float('inf')):
            with self.subTest(budget=budget):
                self.config(image_generation_budget=budget)
                with self.assertRaises(ValueError):rules.policy(self.root)
    def test_non_mexico_policy_rejected(self):
        self.config(site_id='MLB')
        with self.assertRaises(ValueError):rules.policy(self.root)
    def test_budget_checked_before_inspection(self):
        self.config(image_generation_budget=1)
        with patch.object(screening,'_image_bytes',side_effect=AssertionError('must not read')) as read:
            with self.assertRaises(ValueError):self.screen(self.draft())
        read.assert_not_called()
    def test_three_originals_are_candidates_not_approved(self):
        d=self.draft();r,_=self.screen(d)
        self.assertEqual(r[0]['missing_count'],0)
        self.assertFalse(d['evidence']['image_review']['source_screening']['human_approved'])
        self.assertTrue(all(i['review_status']=='AUTO_SCREENED' for i in d['evidence']['image_review']['gallery']))
    def test_shortage_goes_to_existing_input_requests(self):
        r,_=self.screen(self.draft(2));items=rules.shortage_items(r)
        self.assertEqual(items[0]['status'],'skipped');self.assertEqual(items[0]['state'],'BLOCKED_EVIDENCE')
        self.assertEqual(input_requests(items)[0]['sku_ids'],['a'])
    def test_empty_gallery_requires_three(self):
        r,_=self.screen(self.draft(0));self.assertEqual(r[0]['missing_count'],3)
    def test_duplicates_do_not_fill_count(self):
        d=self.draft();d['evidence']['image_review']['source_gallery']*=3
        r,m=self.screen(d);self.assertEqual(m.call_count,3);self.assertEqual(r[0]['usable_count'],3)
    def test_identical_bytes_deduplicated(self):
        d=self.draft()
        with patch('src.source_image_resolution_v3.recover_image',side_effect=lambda r,i:(i,{})):
            r=screening.screen(self.root,d,d['evidence']['sku_details'],inspector=lambda *a:{'usable':True,'fingerprint':'same'})
        self.assertEqual(r[0]['usable_count'],1)
    def test_cap_bounds_inspections_and_outputs(self):
        self.config(max_images_per_sku=3);d=self.draft(10);r,m=self.screen(d)
        self.assertEqual(m.call_count,3);self.assertEqual(r[0]['usable_count'],3)
    def test_failed_inspections_still_bounded(self):
        self.config(max_images_per_sku=3);r,m=self.screen(self.draft(10),{'usable':False})
        self.assertEqual(m.call_count,3);self.assertEqual(r[0]['missing_count'],3)
    def test_generated_sources_never_used(self):
        d=self.draft(image_type='generated',review_status='APPROVED');r,m=self.screen(d)
        self.assertEqual(m.call_count,0);self.assertEqual(r[0]['missing_count'],3)
    def test_generated_legacy_selection_not_counted(self):
        d=self.draft(0);d['evidence']['image_review']['gallery']=[{'local_file':'generated/a.jpg','review_status':'APPROVED'}]
        r,_=self.screen(d);self.assertEqual(r[0]['usable_count'],0)
    def test_rejected_not_counted(self):
        r,m=self.screen(self.draft(review_status='REJECTED'));self.assertEqual(m.call_count,0);self.assertEqual(r[0]['missing_count'],3)
    def test_watermark_wrong_variant_uncertain_rejected(self):
        for key in ('contains_watermark','wrong_variant','uncertain'):
            r,_=self.screen(self.draft(),{'usable':True,key:True})
            self.assertEqual(r[0]['usable_count'],0)
    def test_zying_unreviewed_main_remains_unapproved(self):
        d=self.draft(1,source='zying_detail',review_status='UNREVIEWED');r,_=self.screen(d)
        self.assertEqual(r[0]['missing_count'],2)
        selected=d['evidence']['image_review']['gallery'][0]
        self.assertEqual(selected['review_status'],'AUTO_SCREENED');self.assertEqual(selected['source'],'zying_detail')
        self.assertFalse(d['evidence']['image_review']['source_screening']['human_approved'])
    def test_human_original_status_preserved(self):
        d=self.draft(0);d['evidence']['image_review']['gallery']=[{'local_file':f'uploads/{i}.jpg','review_status':'HUMAN_SELECTED'} for i in range(3)]
        r,m=self.screen(d);self.assertEqual(r[0]['missing_count'],0);self.assertEqual(m.call_count,0)
        self.assertTrue(all(i['review_status']=='HUMAN_SELECTED' for i in d['evidence']['image_review']['gallery']))
    def test_unreviewed_local_is_not_implicitly_qualified(self):
        d=self.draft(0);d['evidence']['image_review']['gallery']=[{'local_file':'uploads/a.jpg','review_status':'UNREVIEWED'}]
        r,_=self.screen(d);self.assertEqual(r[0]['usable_count'],0)
    def test_legacy_automatic_ast_has_no_generation_calls(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/draft_editor.py').read_text('utf-8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_run_full_ai_flow')
        called={n.func.id for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        self.assertFalse(called & {'start_image_generation','start_sku_image_generation','run_job'})

    def test_legacy_shortage_stops_before_copy_pricing_and_generation(self):
        from src import draft_editor as legacy
        path=self.root/'drafts/test-draft.json';full=self.root/'jobs/full.json'
        write(path,self.draft(0));write(full,{'status':'queued'})
        with patch.object(legacy,'_prepare_multi_sku_ai_workflow',return_value={}),patch.object(legacy,'validate',return_value=([],[])),patch.object(legacy,'_apply_ai_content_and_attributes') as content,patch.object(legacy,'refresh_competitor_pricing') as pricing,patch.object(legacy,'start_image_generation') as images,patch.object(legacy,'start_sku_image_generation') as sku_images:
            legacy._run_full_ai_flow(path,self.root,full)
        job=json.loads(full.read_text('utf-8'))
        self.assertEqual(job['completion_state'],'NEEDS_INPUT')
        self.assertEqual(job['input_requests'][0]['sku_ids'],['a'])
        for call in (content,pricing,images,sku_images):call.assert_not_called()

    def test_pipeline_shortage_skips_only_affected_sku(self):
        from src import sku_processing_v3 as pipeline
        from tests.test_auto_sku_processing import fixture
        root=self.root;path=root/'drafts/test-draft.json';full=root/'jobs/full.json'
        draft=fixture();draft['evidence']['sku_details'][0]['ai_content']={'description':'human existing'}
        write(path,draft);write(full,{'status':'queued'})
        calls=[]
        def apply(p,r,f,j):
            d=json.loads(p.read_text('utf-8'));calls.append(d['evidence']['sku_details'][0]['sku_id'])
            d['payload'].update(title='Product',description='Only b',attributes=[]);write(p,d)
        def save(p,j,**kw):j.update(kw);write(p,j)
        legacy=types.SimpleNamespace(ROOT=root,DRAFTS_DIR=root/'drafts',EditorError=ValueError,
            read_json=lambda p:json.loads(p.read_text('utf-8')),_save_full_job=save,
            _apply_ai_content_and_attributes=apply,get_public_settings=lambda r:{},
            query_official_category=Mock(),discover_official_category=Mock(),
            publish_preflight_issues=lambda d:[],refresh_competitor_pricing=Mock(),
            start_image_generation=Mock(side_effect=AssertionError('paid')),
            start_sku_image_generation=Mock(side_effect=AssertionError('paid')))
        pipeline.install(legacy)
        def screen(r,d,skus,**kw):
            d['evidence']['image_generation_review']={'automatically_generated':False}
            return [{'sku_id':'a','usable_count':2,'missing_count':1},{'sku_id':'b','usable_count':3,'missing_count':0}]
        with patch('src.category_guard_v3.prepare'),patch('src.package_recovery_v3.recover'),patch('src.source_image_screening_v3.screen',side_effect=screen):
            legacy._run_full_ai_flow(path,root,full)
        job=legacy.read_json(full);out=legacy.read_json(path)
        self.assertEqual(calls,['b']);self.assertEqual(job['items'][0]['status'],'skipped')
        self.assertTrue(any('a' in i['sku_ids'] for i in job['input_requests']))
        self.assertEqual(job['completion_state'],'NEEDS_INPUT')
        self.assertEqual(out['evidence']['sku_details'][0]['ai_content']['description'],'human existing')
        legacy.start_image_generation.assert_not_called();legacy.start_sku_image_generation.assert_not_called()

if __name__=='__main__':unittest.main()
