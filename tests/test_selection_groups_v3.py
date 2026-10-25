import copy, unittest
from datetime import date
from pathlib import Path
from src import selection_v3 as s
from tests import test_selection_v3 as fixtures

class ProductGroups(unittest.TestCase):
 def setUp(self):
  f=fixtures.SelectionEvidence();f.setUp();self.snap=f.snapshot;self.snap['page_state']={'loading':False,'row_count':1};self.rules=f.r
  self.a=s.normalize(self.snap)[0][0];self.a['image_url']='https://http2.mlstatic.com/D_661726-CBT112386021648_062026-L.jpg';self.a['title']='2 Piezas De Repuesto Universales Para Ruedas De Maleta CL1'
 def other(self,title=None):
  b=copy.deepcopy(self.a);b['item_id']='MLM9999999999';b['price']=566.81
  if title: b['title']=title
  return b
 def test_same_image_title_preserves_all_evidence(self):
  b=self.other('Zz 2 Piezas Repuesto Universal Ruedas Maleta');rows=[self.a,b];before=copy.deepcopy(rows);groups=s.group_products(rows)
  self.assertEqual(len(groups),1);self.assertEqual(groups[0]['listing_count'],2);self.assertEqual(groups[0]['price_min'],180);self.assertEqual(groups[0]['price_max'],566.81);self.assertEqual(rows,before)
  self.assertNotIn('sales_30d',groups[0]);self.assertEqual(groups[0]['status'],'SUSPECTED_SAME_PRODUCT')
 def test_conflicting_pack_model_direction_and_size_stay_separate(self):
  for title in ['4 Piezas De Repuesto Universales Para Ruedas De Maleta CL1','Ruedas De Repuesto Para Maleta Set De 4 Universales CL1','2 Piezas De Repuesto Universales Para Ruedas De Maleta A08']:
   self.assertEqual(len(s.group_products([self.a,self.other(title)])),2)
  for left,right in [('izquierda','derecha'),('40mm','50mm'),('negro','blanco')]:
   a=copy.deepcopy(self.a);a['title']+=' '+left;b=self.other(self.a['title']+' '+right)
   self.assertEqual(len(s.group_products([a,b])),2)
 def test_unknown_spec_cannot_bridge_conflict(self):
  a=copy.deepcopy(self.a);a['title']=a['title'].replace(' CL1','')
  self.assertEqual(len(s.group_products([a,self.a,self.other(self.a['title'].replace('CL1','A08'))])),2)
 def test_cross_site_category_and_currency_stay_separate(self):
  for key,value in [('site_id','MLB'),('currency','BRL'),('category','Other')]:
   b=self.other();b[key]=value;self.assertEqual(len(s.group_products([self.a,b])),2)
 def test_generic_image_different_product_not_grouped(self):
  self.assertEqual(len(s.group_products([self.a,self.other('Mesa plegable de jardin negro grande')])),2)
 def test_different_images_not_assumed_same(self):
  b=self.other();b['image_url']='https://http2.mlstatic.com/D_111111-MLM999999_102026-L.jpg';self.assertEqual(len(s.group_products([self.a,b])),2)
 def test_missing_images_need_exact_long_title(self):
  a=copy.deepcopy(self.a);a['image_url']='';b=self.other();b['image_url']='';self.assertEqual(len(s.group_products([a,b])),1)
  b['title']='Rueda maleta';self.assertEqual(len(s.group_products([a,b])),2)
 def test_conditional_is_not_monthly_or_candidate(self):
  a=copy.deepcopy(self.a);a['sales_30d']=None;a['cumulative_sales_raw']='总量：25 -50';out=s.screen(a,self.rules,date(2026,10,11))
  self.assertEqual(out['state'],'CONDITIONAL_LEAD');self.assertIsNone(out['sales_30d']);self.assertTrue(out['conditional_research_allowed']);self.assertTrue(out['missing'])
 def test_conditional_closed_unsafe_range_date_missing_or_unloaded(self):
  a=copy.deepcopy(self.a);a['sales_30d']=None;a['cumulative_sales_raw']='总量：25-50'
  for raw in ['0-50','25-51','50+','25-50约','50-25','未知']:
   b=copy.deepcopy(a);b['cumulative_sales_raw']=raw;self.assertEqual(s.screen(b,self.rules,date(2026,10,11))['state'],'NEEDS_EVIDENCE')
  for key,value in [('listed_at','2026-09-11'),('page_loading_verified',False),('price',None),('reviews',None)]:
   b=copy.deepcopy(a);b[key]=value;self.assertEqual(s.screen(b,self.rules,date(2026,10,11))['state'],'NEEDS_EVIDENCE')
  rules={**self.rules,'allow_conditional_leads':False};self.assertEqual(s.screen(a,rules,date(2026,10,11))['state'],'NEEDS_EVIDENCE')
 def test_known_monthly_outside_range_not_rescued(self):
  a=copy.deepcopy(self.a);a['sales_30d']=60;a['cumulative_sales_raw']='25-50';self.assertEqual(s.screen(a,self.rules,date(2026,10,11))['state'],'EXCLUDED')
 def test_malformed_reviews_unknown_not_swapped(self):
  self.assertEqual(s.parse_reviews('4.5/66003'),(4.5,66003))
  for raw in ['23/4.8','6/200','4.8/1.5','unknown']:self.assertEqual(s.parse_reviews(raw),(None,None))
 def test_old_batch_gets_groups_without_rewriting_file(self):
  import tempfile,json
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection') as d:
   root=Path(d);path=s.directory(root)/'batches'/('a'*32+'.json');s.write_new(path,{'id':'a'*32,'rows':[self.a,self.other()]});before=path.read_bytes();out=s.get_batch(root,'a'*32)
   self.assertEqual(len(out['groups']),1);self.assertEqual(path.read_bytes(),before)
 def test_analysis_only_uses_one_listing_per_group(self):
  import tempfile,json
  from unittest.mock import patch
  from src import local_model_runtime as local
  (Path(__file__).resolve().parents[1]/'data/v3/selection').mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data/v3/selection') as d:
   root=Path(d);rows=[{**self.a,'state':'CANDIDATE','missing':[]},{**self.other(),'state':'CANDIDATE','missing':[]}];s.write_new(s.directory(root)/'batches'/('a'*32+'.json'),{'id':'a'*32,'rules':self.rules,'rows':rows})
   def respond(root,payload,timeout):
    items=json.loads(payload['input'])['items'];self.assertEqual(len(items),1)
    return {'model':'test','output':[{'content':[{'type':'output_text','text':json.dumps({'items':[{'item_id':items[0]['item_id'],'fit':'UNCERTAIN','reason':'fixture','verify_next':'spec'}]})}]}]}
   with patch.object(local,'responses',side_effect=respond):out=s.analyze(root,{'batch_id':'a'*32})['batch']
   self.assertEqual(len(out['rows']),2);self.assertEqual(sum('ai_fit' in r for r in out['rows']),1)
 def test_boolean_policy_not_truthy_string(self):
  with self.assertRaises(ValueError):s.rules({'allow_conditional_leads':'false'})

if __name__=='__main__':unittest.main()
