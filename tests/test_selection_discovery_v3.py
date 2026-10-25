import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from src import selection_discovery_v3 as d, selection_v3 as s

class DiscoveryLoop(unittest.TestCase):
 def setUp(self):
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection');self.root=Path(self.tmp.name)
 def tearDown(self): self.tmp.cleanup()
 def start(self,mode='focused',max_rounds=3):
  return d.create(self.root,{'mode':mode,'rules':{'leaf_category':'Ruedas'},'query':'ruedas maleta','max_rounds':max_rounds},background=False)['task']
 def choose(self,t,action): return d.plan(self.root,t['id'],lambda p:{'action':action,'reason':'test'})
 def snap(self,req,id='MLM1234567890',image='D_123-L.jpg'):
  return {'url':req['source_url'],'captured_at':s.stamp(),'applied_filters':{'query':req['query'],'site_id':'MLM','warehouse':'自发货','status':'在售'},'page_state':{'loading':False,'row_count':1,'refresh_confirmed':True},'rows':[{'cells':['','','','2 ruedas repuesto maleta\nSKU：'+id,'总量：\n25-50','','180.00\n墨西哥比索','',''+datetime.now().strftime('%Y-%m-%d'),'Generic','0/0','状态：在售\n数量：0\n自发货','1:Equipaje\n2: Ruedas','详情'],'links':[],'image_url':'https://http2.mlstatic.com/'+image}]}
 def collect(self,t,**changes):
  req=d.claim(self.root,{'enabled':True})['request'];v={'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':self.snap(req)};v.update(changes)
  return d.receive(self.root,v,background=False)['task']
 def test_history_same_image_new_listing_not_new_opportunity(self):
  t=self.start();t=self.choose(t,'luggage_wheels');t=self.collect(t)
  t=self.choose(t,'garden_starter');req=d.claim(self.root,{'enabled':True})['request'];snap=self.snap(req,id='MLM1234567891')
  t=d.receive(self.root,{'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':snap},background=False)['task']
  self.assertEqual(t['rounds'][1]['feedback']['new_signals'],0);self.assertEqual(t['rounds'][1]['rows'][0]['novelty'],'SUSPECTED_SEEN_PRODUCT')
 def test_conditional_not_verified_monthly(self):
  t=self.collect(self.choose(self.start(),'luggage_wheels'));x=t['rounds'][0]['rows'][0]
  self.assertEqual(x['state'],'CONDITIONAL_LEAD');self.assertIsNone(x['sales_30d']);self.assertEqual(x['purpose_state'],'待用途复核');self.assertEqual(t['profit_state'],'REAL_PROFIT_UNVERIFIED')
 def test_old_snapshot_rejected(self):
  t=self.choose(self.start(),'luggage_wheels');req=d.claim(self.root,{'enabled':True})['request'];snap=self.snap(req);snap['captured_at']='2026-10-11T00:00:00Z'
  with self.assertRaises(ValueError):d.receive(self.root,{'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':snap},False)
 def test_mismatched_query_rejected(self):
  t=self.choose(self.start(),'luggage_wheels');req=d.claim(self.root,{'enabled':True})['request'];snap=self.snap(req);snap['applied_filters']['query']='wrong'
  with self.assertRaises(ValueError):d.receive(self.root,{'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':snap},False)
 def test_stop_inflight_callback_rejected(self):
  t=self.choose(self.start(),'luggage_wheels');req=d.claim(self.root,{'enabled':True})['request'];d.stop(self.root,{'task_id':t['id']})
  with self.assertRaises(ValueError):d.receive(self.root,{'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':self.snap(req)},False)
 def test_failure_is_not_empty_market(self):
  t=self.collect(self.choose(self.start(),'luggage_wheels'),outcome='ACCESS_FAILED',snapshot=None)
  self.assertEqual(t['status'],'EXECUTOR_BLOCKED');self.assertEqual(t['rounds'][0]['feedback']['outcome'],'ACCESS_FAILED')
 def test_locked_cannot_change_target(self):
  t=self.start('locked');t=self.choose(t,'garden_starter');self.assertEqual(t['status'],'MODEL_ERROR');self.assertIsNone(t['request'])
 def test_seen_direction_not_repeated(self):
  t=self.collect(self.choose(self.start(),'luggage_wheels'));t=self.choose(t,'luggage_wheels');self.assertEqual(t['status'],'MODEL_ERROR')
 def test_last_round_no_false_model_feedback(self):
  t=self.collect(self.choose(self.start(max_rounds=1),'luggage_wheels'));t=d.finish_or_plan(self.root,t['id']);self.assertEqual(t['status'],'BUDGET_STOP');self.assertFalse(t['rounds'][0]['feedback_seen_by_model'])
 def test_empty_loaded_query_can_switch(self):
  t=self.choose(self.start(),'luggage_wheels');req=d.claim(self.root,{'enabled':True})['request'];snap=self.snap(req);snap['rows']=[];snap['page_state']['row_count']=0
  t=d.receive(self.root,{'task_id':t['id'],'request_id':req['id'],'execution':'ASSISTED_BROWSER','outcome':'READ_OK','snapshot':snap},False)['task'];t=self.choose(t,'garden_starter');self.assertEqual(t['status'],'WAITING_COLLECTOR');self.assertTrue(t['rounds'][0]['feedback_seen_by_model'])
 def test_exclusive_run_and_paused_collector(self):
  t=self.start();self.assertIsNone(d.claim(self.root,{'enabled':False})['request'])
  with self.assertRaises(ValueError): self.start()
 def test_empty_directions_rejected(self):
  with self.assertRaises(ValueError): d.create(self.root,{'direction_ids':[]},background=False)
 def test_assisted_heartbeat_not_extension(self):
  d.claim(self.root,{'enabled':True,'execution':'ASSISTED_BROWSER'})
  self.assertFalse(d.overview(self.root)['collector']['connected'])
  d.claim(self.root,{'enabled':True,'execution':'EXTENSION'})
  self.assertTrue(d.overview(self.root)['collector']['enabled'])
 def test_receipts_and_revisions_persist(self):
  t=self.collect(self.choose(self.start(),'luggage_wheels'));self.assertGreater(t['revision'],2);rid=t['rounds'][0]['receipt_id'];r=json.loads((d.folder(self.root)/'receipts'/(rid+'.json')).read_text(encoding='utf-8'));self.assertEqual(r['execution'],'ASSISTED_BROWSER');self.assertEqual(r['request']['query'],'ruedas maleta');self.assertEqual(len(r['sha256']),64)

if __name__=='__main__':unittest.main()
