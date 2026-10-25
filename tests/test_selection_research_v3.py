import copy, json, tempfile, unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from src import selection_v3 as s, selection_research_v3 as r
from tests import test_selection_v3 as fixture_module

class ResearchReplay(unittest.TestCase):
 def setUp(self):
  fixture=fixture_module.SelectionEvidence();fixture.setUp();self.snap=fixture.snapshot
  self.snap['page_state']={'loading':False,'row_count':1}
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection')
  self.root=Path(self.tmp.name)
  with patch.object(s,'date',wraps=date) as day:
   day.today.return_value=date(2026,10,11)
   self.batch=s.run(self.root,{'snapshot':self.snap,'rules':{'leaf_category':'Ruedas'}})['batch']
  self.case=r.create(self.root,{'batch_id':self.batch['id'],'item_id':'MLM1234567890'})['case']
 def tearDown(self): self.tmp.cleanup()
 def evidence(self,value): return {'value':value,'source':'测试夹具：明确假设，不是真实业务数据','kind':'ESTIMATE','observed_at':'2026-10-11T12:00:00Z'}
 def test_review_column_is_rating_before_count(self):
  self.snap['rows'][0]['cells'][10]='4.8/23'
  row=s.normalize(self.snap)[0][0];self.assertEqual(row['reviews'],23)
  self.assertEqual(s.screen(row,s.rules({'leaf_category':'Ruedas'}),date(2026,10,11))['state'],'SECONDARY')
 def test_receipt_hash_reproducible(self):
  receipt=json.loads((s.directory(self.root)/'receipts'/(self.batch['receipt_id']+'.json')).read_text(encoding='utf-8'))
  import hashlib
  self.assertEqual(receipt['sha256'],hashlib.sha256(json.dumps(receipt['snapshot'],ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest())
 def test_unchanged_create_reuses_case(self):
  self.assertEqual(r.create(self.root,{'batch_id':self.batch['id'],'item_id':'MLM1234567890'})['case']['id'],self.case['id'])
 def test_missing_cost_not_zero(self):
  self.assertIsNone(self.case['assessment']['profit']['profit']);self.assertEqual(self.case['profit_state'],'REAL_PROFIT_UNVERIFIED')
 def test_assumed_weight_and_spec_do_not_pass(self):
  new=r.update(self.root,{'revision_id':self.case['id'],'evidence':{'package_weight_g':self.evidence(650),'spec_match':self.evidence('PASS')}})['case']
  self.assertIn('含包装重量g',new['assessment']['supply']['missing']);self.assertIn('同规格核对',new['assessment']['supply']['missing'])
 def test_money_requires_matching_provenance(self):
  f={'mode':'net_proceeds','currency':'MXN','income':100,'cny_per_currency':1,'costs_cny':{k:1 for k in r.pricing.COSTS}}
  src={k:self.evidence(v) for k,v in {'income':100,'cny_per_currency':1,**{'costs_cny.'+k:1 for k in r.pricing.COSTS}}.items()}
  src['income']['value']=999
  with self.assertRaises(ValueError):r.update(self.root,{'revision_id':self.case['id'],'financial_inputs':f,'financial_sources':src})
 def test_reproducible_profit_remains_scenario(self):
  f={'mode':'net_proceeds','currency':'MXN','income':100,'cny_per_currency':1,'costs_cny':{k:1 for k in r.pricing.COSTS}}
  src={k:self.evidence(v) for k,v in {'income':100,'cny_per_currency':1,**{'costs_cny.'+k:1 for k in r.pricing.COSTS}}.items()}
  new=r.update(self.root,{'revision_id':self.case['id'],'financial_inputs':f,'financial_sources':src})['case']
  self.assertEqual(new['assessment']['profit']['profit'],'92.00')
  self.assertEqual(new['assessment']['profit']['status'],'SCENARIO_ESTIMATE')
  self.assertEqual(new['profit_state'],'REAL_PROFIT_UNVERIFIED')
  self.assertEqual(r.get(self.root,self.case['id'])['revision'],1)
  self.assertEqual(new['parent_id'],self.case['id'])
 def test_cross_country_costs_rejected(self):
  with self.assertRaises(ValueError):r.update(self.root,{'revision_id':self.case['id'],'financial_inputs':{'currency':'BRL'}})
 def test_feedback_enters_next_decision(self):
  seen=[]
  def decide(payload):
   seen.append(copy.deepcopy(payload))
   return {'action':'inspect_market' if not payload['observations'] else ('inspect_supply' if 'inspect_supply' in payload['available_tools'] else 'STOP'),'reason':'缺少同规格依据，不猜测'}
  # Freeze the replay date to its captured fixture; real stale receipts stay blocked.
  with patch.object(s,'date',wraps=date) as day:
   day.today.return_value=date(2026,10,11)
   self.assertEqual(r.market(self.case)['status'],'CANDIDATE')
   new=r.investigate(self.root,{'revision_id':self.case['id']},decide)['case']
  self.assertEqual(len(seen),3);self.assertIn('货源链接',seen[2]['observations'][1]['result']['missing'])
  self.assertEqual(list(seen[0]['available_tools']),['inspect_market'])
  self.assertIn('inspect_supply',seen[1]['available_tools']);self.assertEqual(seen[2]['available_tools'],{})
  self.assertEqual(new['run']['tool_calls'],2);self.assertFalse(new['run']['paid_api_used']);self.assertFalse(new['run']['external_collection_performed'])
  self.assertTrue(new['trace'][0]['feedback_seen_by_model']);self.assertEqual(new['run']['status'],'MODEL_STOP')
 def test_stale_market_feedback_requires_stop_without_supply_access(self):
  seen=[]
  def decide(payload):
   seen.append(copy.deepcopy(payload))
   return {'action':'inspect_market' if not payload['observations'] else 'STOP','reason':'页面快照已过期，需要重新取数'}
  with patch.object(s,'date',wraps=date) as day:
   day.today.return_value=date(2026,10,13)
   new=r.investigate(self.root,{'revision_id':self.case['id']},decide)['case']
  self.assertEqual(len(seen),2);self.assertEqual(seen[1]['available_tools'],{})
  self.assertIn('页面快照已过期，需要重新取数',seen[1]['observations'][0]['result']['missing'])
  self.assertEqual([t['action'] for t in new['trace']],['inspect_market','STOP'])
  self.assertEqual(new['run']['tool_calls'],1);self.assertEqual(new['run']['status'],'MODEL_STOP')
  self.assertTrue(new['trace'][0]['feedback_seen_by_model']);self.assertFalse(new['run']['paid_api_used']);self.assertFalse(new['run']['external_collection_performed'])
 def test_ignoring_missing_feedback_cannot_select_supply(self):
  with patch.object(s,'date',wraps=date) as day:
   day.today.return_value=date(2026,10,13)
   new=r.investigate(self.root,{'revision_id':self.case['id']},lambda p:{'action':'inspect_market' if not p['observations'] else 'inspect_supply','reason':'invalid continuation'})['case']
  self.assertEqual(new['run']['status'],'MODEL_ERROR');self.assertEqual(new['run']['tool_calls'],1)
  self.assertEqual(new['trace'][0]['action'],'inspect_market');self.assertEqual(new['trace'][1]['status'],'MODEL_ERROR')
 def test_invented_tool_fails_closed(self):
  new=r.investigate(self.root,{'revision_id':self.case['id']},lambda p:{'action':'publish','reason':'bad'})['case']
  self.assertEqual(new['run']['status'],'MODEL_ERROR');self.assertEqual(new['run']['tool_calls'],0)
 def test_budget_does_not_mean_complete(self):
  with patch.object(r,'execute',return_value={'status':'EVIDENCE_RECORDED','missing':[],'reasons':[]}):
   new=r.investigate(self.root,{'revision_id':self.case['id']},lambda p:{'action':next(iter(p['available_tools'])),'reason':'replay'})['case']
  self.assertEqual(new['run']['status'],'BUDGET_STOP');self.assertEqual(new['run']['tool_calls'],3)
  self.assertFalse(new['trace'][-1]['feedback_seen_by_model']);self.assertEqual(new['assessment']['status'],'NEEDS_EVIDENCE')
 def test_empty_vs_failed_collection(self):
  empty=copy.deepcopy(self.snap);empty['rows']=[];empty['page_state']['row_count']=0
  b=s.run(self.root,{'snapshot':empty,'rules':{}})['batch'];self.assertEqual(sum(b['counts'].values()),0)
  empty['fetch_error']='timeout'
  with self.assertRaises(ValueError):s.run(self.root,{'snapshot':empty,'rules':{}})
 def test_legacy_receipt_not_realtime_proof(self):
  c=copy.deepcopy(self.case);c['receipt_id']=None;c['receipt_verified']=False
  self.assertEqual(r.market(c)['status'],'NEEDS_EVIDENCE')
 def test_stale_revision_cannot_overwrite_new(self):
  r.update(self.root,{'revision_id':self.case['id'],'evidence':{'package_weight_g':self.evidence(100)}})
  with self.assertRaises(ValueError):r.update(self.root,{'revision_id':self.case['id'],'evidence':{'package_weight_g':self.evidence(200)}})
 def test_new_market_receipt_refreshes_same_case(self):
  self.snap['rows'][0]['cells'][4]='总量：10'
  b=s.run(self.root,{'snapshot':self.snap,'rules':{'leaf_category':'Ruedas'}})['batch']
  c=r.create(self.root,{'batch_id':b['id'],'item_id':'MLM1234567890'})['case']
  self.assertEqual(c['case_id'],self.case['case_id']);self.assertEqual(c['revision'],2)
  self.assertIsNone(c['item']['sales_30d']);self.assertEqual(c['assessment']['market']['status'],'NEEDS_EVIDENCE')
 def test_future_source_and_bad_dimensions_rejected(self):
  for e in [{'package_weight_g':{**self.evidence(10),'observed_at':'2099-01-01T00:00:00Z'}},{'package_dimensions_cm':self.evidence('0x2x3')}]:
   with self.assertRaises(ValueError):r.update(self.root,{'revision_id':self.case['id'],'evidence':e})
if __name__=='__main__':unittest.main()
