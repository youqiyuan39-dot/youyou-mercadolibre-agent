import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src import store_operations_v3 as ops

class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.row={'id':'s1','alias':'店一','merchant_id':1,'auth_type':'CBT'}
        self.reg=patch.object(ops,'registry',return_value=[self.row]);self.reg.start()
    def tearDown(self):self.reg.stop();self.tmp.cleanup()
    def test_open_does_not_request(self):
        with patch.object(ops,'client_for',side_effect=AssertionError('不应请求')):
            self.assertEqual(ops.saved(self.root,'s1','orders')['total'],None)
    def test_failed_refresh_keeps_old_and_marks_stale(self):
        ops.write(self.root,'s1','orders',{'state':'SUCCESS','rows':[{'id':7}],'total':1,'time':'old'})
        with patch.object(ops,'client_for',side_effect=RuntimeError('连接失败')):
            d=ops.refresh(self.root,'s1','orders')
        self.assertEqual(d['rows'][0]['id'],7)
        self.assertEqual(d['last_attempt']['state'],'FAILED')
    def test_pagination(self):
        class Client:
            def get(self,p,q):return {'results':list(range(q['offset'],min(q['offset']+50,65))),'paging':{'total':65}}
        rows,total=ops.search(Client(),'/x',{})
        self.assertEqual((len(rows),total),(65,65))
    def test_official_zero_with_null_questions(self):
        class Client:
            def get(self,p,q):return {'questions':None,'total':0}
        self.assertEqual(ops.search(Client(),'/x',{},'questions'),([],0))
    def test_discount_cannot_cross_store(self):
        ops.write(self.root,'s1','listings',{'time':'x','rows':[{'id':'X','title':'P','price':10,'currency_id':'USD'}]})
        with self.assertRaises(ValueError):ops.discount_preview(self.root,{'store_id':'s1','item_ids':['OTHER'],'discount':10})
        self.assertEqual(ops.discount_preview(self.root,{'store_id':'s1','item_ids':['X'],'discount':10})['rows'][0]['discounted'],'9.00')
    def test_orders_drop_sensitive_fields(self):
        class Client:
            def get(self,p,q=None):
                return {'results':[{'id':9,'seller':{'id':1},'buyer':{'email':'secret'},'payments':[{'card':'secret'}],'status':'paid'}],'paging':{'total':1}}
        self.row['auth_type']='MLM'
        with patch.object(ops,'client_for',return_value=(Client(),{'id':1})):
            d=ops.refresh(self.root,'s1','orders')
        self.assertNotIn('secret',json.dumps(d))
    def test_messages_do_not_mark_read(self):
        ops.write(self.root,'s1','orders',{'state':'SUCCESS','rows':[{'pack_id':2,'seller_id':3}]})
        called=[]
        class Client:
            def get(self,p,q):called.append(q);return {'messages':[],'paging':{'total':0}}
        with patch.object(ops,'client_for',return_value=(Client(),{'id':1})):
            ops.refresh(self.root,'s1','messages')
        self.assertEqual(called[0]['mark_as_read'],'false')
    def test_bad_vault_identity_stops(self):
        with patch.object(ops.oauth,'load_private_json',return_value={'access_token':'test'}),patch.object(ops,'MercadoLibreClient') as c:
            c.return_value.me.return_value={'id':2}
            with self.assertRaises(ValueError):ops.client_for(self.root,self.row)

if __name__=='__main__':unittest.main()
