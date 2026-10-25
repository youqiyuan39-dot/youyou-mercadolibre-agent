import copy,json,tempfile,unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from src import selection_v3 as s,local_model_runtime as local

class SelectionEvidence(unittest.TestCase):
 def setUp(self):
  self.today=date(2026,10,11)
  self.snapshot={'captured_at':'2026-10-11T12:00:00Z','url':'https://meli.zying.net/#/goods/total','rows':[{'cells':['','','预览','Ruedas de repuesto\nSKU：MLM1234567890\n1688同款','总量：\n1.2万\n30天：\n5','','10.00\n美元\n180.00\n墨西哥比索','8.00\n美元\n144.00\n墨西哥比索','2026-10-08','Generic','0/0','状态：在售\n数量：3\n自发货','1:Equipaje\n2: Ruedas','详情'],'links':[{'href':'https://articulo.mercadolibre.com.mx/MLM-1234567890-ejemplo-_JM'}]}]}
  self.r=s.rules({'leaf_category':'Ruedas'})
 def row(self):return s.normalize(self.snapshot)[0][0]
 def test_good_candidate_is_not_profit_verified(self):
  out=s.screen(self.row(),self.r,self.today);self.assertEqual(out['state'],'CANDIDATE');self.assertEqual(out['profit_state'],'REAL_PROFIT_UNVERIFIED')
 def test_missing_monthly_never_uses_cumulative(self):
  self.snapshot['rows'][0]['cells'][4]='总量：\n1000000+'
  row=self.row();self.assertIsNone(row['sales_30d']);self.assertEqual(s.screen(row,self.r,self.today)['state'],'NEEDS_EVIDENCE')
 def test_ranges_not_exact(self):
  for raw in ['1-50','1.2万+','5000 -1.00 万']:
   self.assertIsNone(s.number(raw))
  self.assertEqual(s.number('1.2万'),12000)
 def test_mixed_country_rejected_even_price_fits(self):
  row=self.row();row['site_id']='MLB';row['currency']='BRL';self.assertEqual(s.screen(row,self.r,self.today)['state'],'EXCLUDED')
 def test_site_filter_does_not_override_record(self):
  row=self.row();row['listed_at']='2022-07-25';self.assertEqual(s.screen(row,self.r,self.today)['state'],'EXCLUDED')
 def test_closed_and_zero_stock_differ(self):
  row=self.row();row['stock']=0;self.assertEqual(s.screen(row,self.r,self.today)['state'],'CANDIDATE')
  row['status']='缺货';self.assertEqual(s.screen(row,self.r,self.today)['state'],'EXCLUDED')
 def test_reviews_secondary_not_false_hard_rejection(self):
  row=self.row();row['reviews']=25;self.assertEqual(s.screen(row,self.r,self.today)['state'],'SECONDARY')
 def test_leaf_required(self): self.assertEqual(s.screen(self.row(),s.rules({}),self.today)['state'],'NEEDS_EVIDENCE')
 def test_duplicate_listing_deduplicated_not_title(self):
  row=copy.deepcopy(self.snapshot['rows'][0]);self.snapshot['rows']+=[row];rows,dups=s.normalize(self.snapshot);self.assertEqual(len(rows),1);self.assertEqual(len(dups),1)
  row['cells'][3]=row['cells'][3].replace('MLM1234567890','MLM1234567891');rows,_=s.normalize(self.snapshot);self.assertEqual(len(rows),2)
 def test_stale_future_dates(self):
  row=self.row();row['source_captured_at']='2026-09-11T12:00:00Z';self.assertEqual(s.screen(row,self.r,self.today)['state'],'NEEDS_EVIDENCE')
  row['source_captured_at']='2026-10-11T12:00:00Z';row['listed_at']='2026-10-12';self.assertEqual(s.screen(row,self.r,self.today)['state'],'NEEDS_EVIDENCE')
 def test_model_unknown_duplicate_missing_ids_rejected(self):
  for ids in [['MLM9'],[],['MLM1','MLM1']]:
   with self.assertRaises(ValueError):s.check_ai({'items':[{'item_id':i,'fit':'MATCH'} for i in ids]},['MLM1'])
 def test_local_only_analysis_preserves_rule_status(self):
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection') as d:
   root=Path(d)
   with patch.object(s,'date') as day:
    day.today.return_value=self.today
    batch=s.run(root,{'snapshot':self.snapshot,'rules':{}})['batch']
   output={'items':[{'item_id':'MLM1234567890','fit':'MATCH','reason':'用途可能符合','verify_next':'包装重量未知'}]}
   response={'model':'local-test','output':[{'content':[{'type':'output_text','text':json.dumps(output)}]}]}
   with patch.object(local,'responses',return_value=response) as fn: new=s.analyze(root,{'batch_id':batch['id']})['batch']
   self.assertFalse(new['paid_api_used']);self.assertEqual(new['rows'][0]['state'],'NEEDS_EVIDENCE');self.assertNotEqual(new['id'],batch['id']);self.assertTrue((s.directory(root)/'batches'/(batch['id']+'.json')).is_file())
 def test_rule_evidence_sent_to_model_without_state_override(self):
  self.snapshot['rows'][0]['cells'][4]='总量：\n25-50'
  self.snapshot['page_state']={'loading':False,'row_count':1}
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection') as d:
   root=Path(d)
   with patch.object(s,'date') as day:
    day.today.return_value=self.today
    day.fromisoformat.side_effect=date.fromisoformat
    batch=s.run(root,{'snapshot':self.snapshot,'rules':{'leaf_category':'Ruedas'}})['batch']
   output={'items':[{'item_id':'MLM1234567890','fit':'MISMATCH','reason':'用途不匹配','verify_next':'复核用途','state':'EXCLUDED'}]}
   response={'model':'local-test','output':[{'content':[{'type':'output_text','text':json.dumps(output)}]}]}
   with patch.object(local,'responses',return_value=response) as fn:
    new=s.analyze(root,{'batch_id':batch['id']})['batch']
   payload=json.loads(fn.call_args.args[1]['input'])
   evidence=payload['items'][0]['rule_evidence']
   self.assertEqual(evidence['state'],'CONDITIONAL_LEAD')
   self.assertIsNone(evidence['sales_30d'])
   self.assertEqual(evidence['cumulative_sales_interval'],[25,50])
   self.assertEqual(new['rows'][0]['state'],'CONDITIONAL_LEAD')
   self.assertIsNone(new['rows'][0]['sales_30d'])
   self.assertEqual(new['rows'][0]['profit_state'],'REAL_PROFIT_UNVERIFIED')
   self.assertEqual(s.get_batch(root,batch['id'])['ai_state'],'NOT_RUN')
 def test_loading_and_partial_page_rejected(self):
  for state in [{'loading':True,'row_count':1},{'loading':False,'row_count':2},{'loading':'false','row_count':1}]:
   self.snapshot['page_state']=state
   with self.assertRaises(ValueError):s.normalize(self.snapshot)
  self.snapshot['page_state']={'loading':False,'row_count':1};self.assertEqual(len(s.normalize(self.snapshot)[0]),1)
 def test_history_sorted_by_timestamp_not_random_uuid(self):
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection') as d:
   root=Path(d)
   for bid,stamp in [('f'*32,'2026-10-10T12:00:00Z'),('0'*32,'2026-10-11T12:00:00Z')]:
    s.write_new(s.directory(root)/'batches'/(bid+'.json'),{'id':bid,'created_at':stamp,'query':'test','counts':{},'site_id':'MLM'})
   self.assertEqual(s.list_batches(root)['items'][0]['id'],'0'*32)
 def test_bad_source_paths_and_negative_limits(self):
  for path in ['../secret.json','C:\\data.json','a/b.json']:
   with self.assertRaises(ValueError):s.get_source(Path('.'),path)
  with self.assertRaises(ValueError):s.rules({'sales_min':-1})
if __name__=='__main__':unittest.main()
