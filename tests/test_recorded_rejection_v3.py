import copy
from pathlib import Path
import tempfile
import unittest
from src import target_publish_v3 as p, publish_service as service


class RejectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.pid='a'*32
        self.request=[{'attributes':[{'id':'SELLER_SKU','value_name':'sku1'}],
                       'sites_to_sell':[{'site_id':'MLM','logistic_type':'remote'}]}]
        self.response=[{'status':400,'error':'validation_error','message':'Validation error',
                        'cause':[{'code':'pictures.invalid','message':'image has invalid dimensions'}]}]
        self.record={'draft':'one-draft.json','store':{'id':'A'},'plan_id':self.pid,
            'status':'pending_confirmation','variants':service._attempt_variants(self.request,[]),
            'attempts':[{'plan_id':self.pid,'endpoint':'/global/user-products/families','response':self.response}]}
        p.save(self.root/'data/v3/publishing/plans'/(self.pid+'.json'),
               {'name':self.record['draft'],'store':self.record['store'],'request':self.request})

    def test_per_item_400_is_failed_not_pending(self):
        rows=service._attempt_variants(self.request,self.response)
        self.assertEqual(rows[0]['sites'][0]['status'],'failed')
        self.assertIn('invalid dimensions',rows[0]['sites'][0]['error'])

    def test_unknown_server_failure_and_absent_result_stay_pending(self):
        for response in ([],[{'status':500,'error':'server_error'}], [{}]):
            with self.subTest(response=response):
                self.assertEqual(service._attempt_variants(self.request,response)[0]['sites'][0]['status'],'pending_confirmation')

    def test_exact_frozen_rejection_can_reconcile_locally(self):
        self.assertTrue(p.reconcile_recorded_rejection(self.root,self.record))
        self.assertEqual(self.record['status'],'failed')
        self.assertTrue(self.record['creation_rejected'])

    def test_ambiguous_or_created_result_never_clears_unknown(self):
        for changes in ({'outcome_unknown':True},{'request_in_flight':True},
                        {'variants':[{'seller_sku':'sku1','siteless_user_product_id':'CBTU1','sites':[]}]}):
            current={**copy.deepcopy(self.record),**changes};before=copy.deepcopy(current)
            self.assertFalse(p.reconcile_recorded_rejection(self.root,current))
            self.assertEqual(current,before)

    def test_plan_store_and_sku_scope_mismatch_preserve_record(self):
        for changes in ({'store':{'id':'B'}},{'draft':'other-draft.json'},
                        {'variants':[{'seller_sku':'other','sites':[{'site_id':'MLM'}]}]}):
            current={**copy.deepcopy(self.record),**changes};before=copy.deepcopy(current)
            self.assertFalse(p.reconcile_recorded_rejection(self.root,current))
            self.assertEqual(current,before)

    def test_response_with_any_created_identifier_is_not_definite_rejection(self):
        result={**self.response[0],'siteless_user_product_id':'CBTU1'}
        self.assertFalse(service._definite_rejection(result))


if __name__=='__main__':unittest.main()
