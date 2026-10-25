import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src import integrated_pricing_v3 as m
from src import workbench_assistant as a
from src.assistant_business_knowledge import lookup

class MainlineTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.root=Path(self.temp.name);(self.root/'drafts').mkdir();self.path=self.root/'drafts/test-draft.json'
  self.path.write_text(json.dumps({'payload':{'title':'Llave ajustable','sites_to_sell':[{'site_id':'MLM'}]},'evidence':{}}),encoding='utf-8')
  # The mocked apply still owns a persisted plan, as the production API does.
  plans=self.root/'data/v3/pricing/plans';plans.mkdir(parents=True);(plans/('a'*32+'.json')).write_text(json.dumps(self.plan()),encoding='utf-8')
 def tearDown(self):self.temp.cleanup()
 def plan(self):return {'id':'a'*32,'state':'READY_ESTIMATED','rows':[{'sku_id':'white','margin_pct':'30'},{'sku_id':'bag','margin_pct':'30'}],'errors':[]}
 def test_full_sku_budget_applied_once_without_publishing(self):
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare',return_value=self.plan()) as prep,patch.object(m.pricing,'apply') as apply:
   r=m.run(self.path,self.root)
  self.assertEqual(r['state'],'BUDGET_ESTIMATE');self.assertEqual(len(r['rows']),2)
  self.assertNotIn('sku_id',prep.call_args.args[1]);apply.assert_called_once_with(self.root,'a'*32,True)
  self.assertEqual(m.summary(json.loads(self.path.read_text(encoding='utf-8')),self.root)['rows'],r['rows'])
 def test_missing_cost_never_applies_or_continues(self):
  plan=self.plan();plan.update(state='BLOCKED',errors=['包装重量未确认'])
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare',return_value=plan),patch.object(m.pricing,'apply') as apply:
   with self.assertRaisesRegex(ValueError,'包装重量'):m.run(self.path,self.root)
  apply.assert_not_called();self.assertEqual(m.summary(json.loads(self.path.read_text(encoding='utf-8')),self.root)['state'],'BLOCKED')
 def test_unsupported_site_never_silently_skipped(self):
  d=json.loads(self.path.read_text(encoding='utf-8'));d['payload']['sites_to_sell'].append({'site_id':'MLB'});self.path.write_text(json.dumps(d),encoding='utf-8')
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare') as prep:
   with self.assertRaisesRegex(ValueError,'仅支持墨西哥'):m.run(self.path,self.root)
  prep.assert_not_called()
 def test_query_failure_preserves_original_price(self):
  before=self.path.read_text(encoding='utf-8')
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare',side_effect=ValueError('本次未取得市场价格')),patch.object(m.pricing,'apply') as apply:
   with self.assertRaisesRegex(ValueError,'本次未取得'):m.run(self.path,self.root)
  apply.assert_not_called();self.assertEqual(json.loads(before)['payload'],json.loads(self.path.read_text(encoding='utf-8'))['payload'])
 def test_knowledge_separated_from_live_tools(self):
  self.assertTrue(a.knowledge_question('讲解选品知识和利润原则'));self.assertFalse(a.knowledge_question('这件商品卡在哪里'))
  self.assertIn('selection',lookup('选品知识')['guides']);self.assertEqual(lookup('使用教程')['kind'],'GENERAL_GUIDANCE')
  self.assertIn('get_business_knowledge',a.TOOLS);self.assertNotIn('get_selection_batch',a.TOOLS)
 def test_price_changes_invalidate_summary(self):
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare',return_value=self.plan()),patch.object(m.pricing,'apply'):m.run(self.path,self.root)
  draft=json.loads(self.path.read_text(encoding='utf-8'));draft['pricing_plan']={'domestic_shipping_cny':99}
  self.assertEqual(m.summary(draft)['state'],'STALE')
 def test_full_flow_runs_pricing_before_images_and_finishes(self):
  self.check_full_flow(False)
 def test_full_flow_stops_before_images_when_pricing_blocked(self):
  self.check_full_flow(True)
 def check_full_flow(self,blocked):
  from src import draft_editor as legacy
  # Automatic flow requires qualified original SKU images and never generates.
  d=json.loads(self.path.read_text(encoding='utf-8'))
  d['evidence']['sku_details']=[{'sku_id':'white'}]
  d['evidence']['image_review']={'gallery':[{'local_file':f'uploads/{i}.jpg','review_status':'HUMAN_SELECTED'} for i in range(3)]}
  self.path.write_text(json.dumps(d),encoding='utf-8')
  full=self.root/'jobs/test-full-ai-job.json';full.parent.mkdir();full.write_text('{"status":"queued"}',encoding='utf-8')
  plan=self.plan()
  if blocked:plan.update(state='BLOCKED',errors=['缺采购价'])
  with patch.object(m.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(m.pricing,'prepare',return_value=plan),patch.object(m.pricing,'apply'),patch.object(legacy,'_prepare_multi_sku_ai_workflow',return_value={'mode':'SINGLE_SKU'}),patch.object(legacy,'validate',return_value=([],[])),patch.object(legacy,'_apply_ai_content_and_attributes'),patch.object(legacy,'refresh_competitor_pricing',side_effect=m.run),patch.object(legacy,'get_public_settings',return_value={'strategy':{}}),patch.object(legacy,'start_image_generation') as images,patch.object(legacy,'_wait_child_job'):
   legacy._run_full_ai_flow(self.path,self.root,full)
  job=json.loads(full.read_text(encoding='utf-8'))
  self.assertEqual(job['status'],'failed' if blocked else 'done')
  if blocked:images.assert_not_called();self.assertIn('缺采购价',job['error'])
  else:images.assert_not_called();self.assertEqual(m.summary(json.loads(self.path.read_text(encoding='utf-8')),self.root)['state'],'BUDGET_ESTIMATE')
 def test_history_unverified(self):self.assertEqual(m.summary({})['state'],'NOT_RUN')
 def test_ui_no_selection_entry_and_price_review_present(self):
  base=Path(__file__).resolve().parents[1]
  html=(base/'web/workbench_v3.html').read_text(encoding='utf-8');self.assertNotIn('href="#selection"',html);self.assertNotIn('src="/selection_v3.js"',html)
  js=(base/'web/publish.js').read_text(encoding='utf-8');self.assertRegex(js,r'pricingReview\(p\.ai_processing_pricing(?:,true)?\)');self.assertIn('pricingReview(r.ai_processing_pricing)',js)
class CostOnlyTests(unittest.TestCase):
 def test_only_explicit_opt_in_allows_absent_market_reference(self):
  from tests.test_autopricing_v3 import draft
  from src import autopricing_v3 as a
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   root=Path(folder);(root/'drafts').mkdir();(root/'drafts/demo-draft.json').write_text(json.dumps(draft()),encoding='utf-8')
   with patch.object(a.prices.operations,'store',return_value={'id':'s'}),patch.object(a.prices,'compare',return_value={'state':'UNAVAILABLE','items':[],'price_bands':[]}),patch.object(a.prices,'rate_quote',return_value={'cny_per_currency':'6.6921','source':{}}):
    args={'name':'demo-draft.json','store_id':'s','query':'wrench'}
    blocked=a.prepare(root,args);ready=a.prepare(root,{**args,'allow_cost_only':True})
   self.assertEqual(blocked['state'],'BLOCKED');self.assertEqual(ready['state'],'MARGIN_POLICY_PENDING');self.assertTrue(ready['rows']);self.assertTrue(ready['warnings']);self.assertEqual(ready['method'],'COST_TARGET_MARKET_UNAVAILABLE');self.assertEqual(ready['comparison']['items'],[])
 def test_budget_review_required_before_any_publish_access(self):
  import time
  from src import target_publish_v3 as publishing
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   root=Path(folder);path=root/'data/v3/publishing/plans';path.mkdir(parents=True)
   from src.autopricing_v3 import policy_fingerprint
   receipt=path/('a'*32+'.json')
   receipt.write_text(json.dumps({'state':'ready','valid':True,'expires_at':time.time()+60,'pricing_policy_hash':policy_fingerprint(root),'ai_processing_pricing':{'requires_budget_review':True}}),encoding='utf-8')
   before=receipt.read_bytes()
   # Passing the policy gate must still stop before store/private/API access.
   with patch.object(publishing,'store_row') as store,patch.object(publishing,'_account_checks') as api,patch.object(publishing.service,'_upload_pictures') as upload:
    for confirmed in (False,None,'true',1):
     with self.subTest(budget_review_confirmed=confirmed),self.assertRaisesRegex(publishing.Error,'核对预算利润'):
      publishing.commit(root,'a'*32,True,confirmed)
    store.assert_not_called();api.assert_not_called();upload.assert_not_called()
   self.assertEqual(receipt.read_bytes(),before)
 def test_missing_or_changed_policy_blocks_before_publish_access(self):
  import time
  from src import target_publish_v3 as publishing
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);path=root/'data/v3/publishing/plans';path.mkdir(parents=True)
   receipt=path/('b'*32+'.json')
   for extra in ({},{'pricing_policy_hash':'obsolete-policy'}):
    receipt.write_text(json.dumps({'state':'ready','valid':True,'expires_at':time.time()+60,'ai_processing_pricing':{'requires_budget_review':True},**extra}),encoding='utf-8')
    before=receipt.read_bytes()
    with self.subTest(policy=extra),patch.object(publishing,'store_row') as store,patch.object(publishing,'_account_checks') as api,patch.object(publishing.service,'_upload_pictures') as upload:
     with self.assertRaisesRegex(publishing.Error,'利润政策已变化或旧计划缺少政策版本'):
      publishing.commit(root,'b'*32,True,True)
     store.assert_not_called();api.assert_not_called();upload.assert_not_called()
    self.assertEqual(receipt.read_bytes(),before)
if __name__=='__main__':unittest.main()
