import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import autonomous_flow_v3 as flow
from src.autonomous_workbench_adapter import WorkbenchAdapter


class AdapterTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.legacy=Mock()
        self.task=flow.enqueue(self.root,'one-draft.json','source',{'store_id':'x','site_id':'MLM'},{'reference':'user'})
        flow.write(self.root/'data/intake/source.json',{'title':'source'})
        flow.write(self.root/'drafts/one-draft.json',{'evidence':{'source_capture':'data/intake/source.json','sku_details':[{'sku_id':'1'}],'supplier_page':{'title':'source'}}})
        self.adapter=WorkbenchAdapter(self.root,self.legacy)
        self.task['artifacts']['evidence']=self.adapter('evidence',self.task)['artifact']

    def test_processing_start_then_wait_does_not_dispatch_twice(self):
        result=self.adapter('process',self.task)
        self.assertEqual(result['outcome'],'wait')
        flow.write(self.root/'jobs/one-draft-full-ai-job.json',{'status':'running'})
        self.task['artifacts']['process']=result['artifact']
        self.assertEqual(self.adapter('process',self.task)['outcome'],'wait')
        self.legacy.start_full_ai_flow.assert_called_once()

    def test_live_receipt_requires_site_level_active_state(self):
        with patch('src.autonomous_workbench_adapter.publishing.refresh') as refresh:
            refresh.return_value={'record':{'status':'success','variants':[]}}
            self.assertEqual(self.adapter('reconcile',self.task)['outcome'],'wait')
            refresh.return_value={'record':{'status':'success','variants':[{'sites':[{'site_id':'MLM','item_id':'MLM1','marketplace_status':'active','checked_at':123}]}]}}
            self.assertTrue(self.adapter('reconcile',self.task)['artifact']['verified_live'])

    def test_no_authorization_never_calls_commit(self):
        with patch('src.autonomous_workbench_adapter.publishing.commit') as commit:
            self.assertEqual(self.adapter('publish',self.task)['outcome'],'blocked')
            commit.assert_not_called()

    def test_changed_source_never_starts_processing(self):
        flow.write(self.root/'data/intake/source.json',{'title':'different product'})
        self.assertEqual(self.adapter('process',self.task)['reason'],'SOURCE_CAPTURE_CHANGED')
        self.legacy.start_full_ai_flow.assert_not_called()

    def test_legacy_task_without_snapshot_cannot_publish(self):
        self.task['artifacts'].pop('evidence')
        with patch('src.autonomous_workbench_adapter.publishing.commit') as commit:
            self.assertEqual(self.adapter('publish',self.task)['reason'],'SOURCE_CAPTURE_BINDING_REQUIRED')
            commit.assert_not_called()

    def test_missing_capture_is_not_processing_success(self):
        self.task['artifacts'].pop('evidence')
        flow.write(self.root/'drafts/one-draft.json',{'evidence':{'supplier_page':{'title':'x'},'sku_details':[{}]}})
        self.assertEqual(self.adapter('evidence',self.task)['reason'],'IMMUTABLE_SOURCE_CAPTURE_REQUIRED')

    def _validate_pricing(self, **changes):
        pricing={'state':'BUDGET_ESTIMATE','errors':[],'reference_issues':[],
                 'requires_budget_review':False,'rows':[{'sku_id':'1','floor_passed':True}],
                 'store_id':'x','site_id':'MLM',**changes}
        with patch('src.autonomous_workbench_adapter.publishing._local_issues',return_value=[]), \
             patch('src.integrated_pricing_v3.summary',return_value=pricing):
            return self.adapter('validate',self.task)

    def test_stale_and_incomplete_pricing_never_reuses_old_pass_flags(self):
        for state in ('STALE','BLOCKED','NOT_RUN','PARTIAL_BUDGET_ESTIMATE',
                      'MARGIN_POLICY_PENDING','MANUAL_PRICE_LOCKED',None,'UNKNOWN'):
            with self.subTest(state=state):
                result=self._validate_pricing(state=state)
                self.assertEqual(result['outcome'],'blocked')
                self.assertEqual(result['reason'],'PROFIT_EVIDENCE_REQUIRED')

    def test_pricing_errors_and_reference_errors_block_old_pass(self):
        for changes in ({'errors':['cost evidence missing']},
                        {'reference_issues':[{'code':'REFERENCE_STALE'}]}):
            with self.subTest(changes=changes):
                self.assertEqual(self._validate_pricing(**changes)['reason'],'PROFIT_EVIDENCE_REQUIRED')

    def test_no_complete_sku_profit_rows_blocks_validation(self):
        for rows in ([],None,{},[{}],[{'floor_passed':False}],
                     [{'floor_passed':True},{'floor_passed':False}],['malformed']):
            with self.subTest(rows=rows):
                self.assertEqual(self._validate_pricing(rows=rows)['reason'],'PROFIT_EVIDENCE_REQUIRED')

    def test_missing_or_truthy_budget_review_never_means_review_complete(self):
        for flag in (None,True,0,'false'):
            with self.subTest(flag=flag):
                self.assertEqual(self._validate_pricing(requires_budget_review=flag)['reason'],'PROFIT_EVIDENCE_REQUIRED')

    def test_pricing_target_mismatch_still_blocks_current_complete_estimate(self):
        for changes in ({'store_id':'another-store'},{'site_id':'MLB'}):
            with self.subTest(changes=changes):
                self.assertEqual(self._validate_pricing(**changes)['reason'],'PRICING_TARGET_MISMATCH')

    def _set_publish_skus(self, skus, payload=None):
        flow.write(self.root/'drafts/one-draft.json',{'payload':payload or {},
                   'evidence':{'source_capture':'data/intake/source.json',
                               'sku_details':skus,'supplier_page':{'title':'source'}}})

    def test_profit_rows_must_cover_every_real_publish_sku_exactly_once(self):
        self._set_publish_skus([{'sku_id':'1'},{'sku_id':'2'}])
        for ids in (['1'],['1','1'],['1','2','3'],['1',None],['1',''],['1',True],['1',' ']):
            with self.subTest(ids=ids):
                rows=[{'sku_id':sid,'floor_passed':True} for sid in ids]
                self.assertEqual(self._validate_pricing(rows=rows)['reason'],'PRICING_SKU_COVERAGE_MISMATCH')
        self.assertEqual(self._validate_pricing(rows=[{'floor_passed':True}])['reason'],
                         'PRICING_SKU_COVERAGE_MISMATCH')

    def test_missing_or_duplicate_publish_sku_identity_is_not_auto_generated_for_profit(self):
        for skus in ([{}],[{'sku_id':'1'},{'sku_id':'1'}],[{'sku_id':False}]):
            with self.subTest(skus=skus):
                self._set_publish_skus(skus)
                self.assertEqual(self._validate_pricing()['reason'],'PRICING_SKU_COVERAGE_MISMATCH')

    def test_complete_single_and_multi_sku_coverage_remain_valid(self):
        self.assertEqual(self._validate_pricing()['outcome'],'passed')
        self._set_publish_skus([{'sku_id':'1'},{'sku_id':2}])
        rows=[{'sku_id':'2','floor_passed':True},{'sku_id':'1','floor_passed':True}]
        self.assertEqual(self._validate_pricing(rows=rows)['outcome'],'passed')

    def test_legacy_single_sku_coverage_uses_publication_service_fallback(self):
        self._set_publish_skus([],{'seller_sku':'legacy-seller-sku'})
        self.assertEqual(self._validate_pricing()['reason'],'PRICING_SKU_COVERAGE_MISMATCH')
        rows=[{'sku_id':'legacy-seller-sku','floor_passed':True}]
        self.assertEqual(self._validate_pricing(rows=rows)['outcome'],'passed')


if __name__=='__main__':unittest.main()
