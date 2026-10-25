import copy,json,tempfile,unittest
from pathlib import Path
from src.attribute_context_v3 import editable,valid_value
from src.sku_processing_v3 import bind_targets
from src.source_intake import normalize_collection
from src.project_knowledge_v3 import retrieve

ROOT=Path(__file__).resolve().parents[1]
class ProjectRepairs(unittest.TestCase):
 def test_targets_shared_without_price_or_other_sku_mutation(self):
  d={'payload':{'sites_to_sell':[{'site_id':'MLM','logistic_type':'remote','price':99,'net_proceeds':44}]},'evidence':{'sku_details':[{'sku_id':'a'},{'sku_id':'b','site_pricing':[{'site_id':'MLB','net_proceeds':13}]}]}}
  before=copy.deepcopy(d['evidence']['sku_details'][1]);bind_targets(d)
  self.assertEqual(d['evidence']['sku_details'][0]['site_pricing'],[{'site_id':'MLM','logistic_type':'remote'}])
  self.assertEqual(d['evidence']['sku_details'][1],before)
 def test_internal_definitions_and_labels_are_not_values(self):
  self.assertFalse(editable({'id':'X','tags':['hidden','read_only']}))
  self.assertTrue(editable({'id':'BRAND','tags':['required']}))
  for a in [{'id':'X','tags':{'hidden':True}},{'id':'X','tags':{'read_only':True}},{'id':'PACKAGE_LENGTH'},{'id':'CATALOG_TITLE'}]:self.assertFalse(editable(a))
  self.assertFalse(valid_value({'value_name':'Material'},{'id':'MATERIAL','name':'Material'}))
  self.assertFalse(valid_value({'value_name':'Weight'},{'id':'WEIGHT','value_type':'number_unit'}))
  self.assertTrue(valid_value({'value_name':'120 g'},{'id':'WEIGHT','value_type':'number_unit'}))
 def test_bound_shared_image_survives_intake(self):
  from tests.test_source_intake import SourceIntakeTests
  case=SourceIntakeTests();case.setUp();p=copy.deepcopy(case.payload)
  url='https://cbu01.alicdn.com/product-1.jpg';p['sku_image_sets']=[{'sku_id':'sku-1','variant':'短款','images':[url]}]
  self.assertEqual(normalize_collection(p)['sku_image_sets'][0]['images'],[url])
 def test_project_retrieval_latest_policy_and_bounded_context(self):
  # Deterministic local retrieval: no dependency on the live 18081 service.
  r=retrieve(ROOT,'利润定价跟价10%净收益怎么计算',semantic=False)
  self.assertEqual(r['retrieval_status'],'MATCHED')
  self.assertIn('pricing',r['guides']);self.assertLessEqual(r['context_chars'],5000)
  docs={d['id']:d for d in json.loads((ROOT/'knowledge/project/knowledge.json').read_text(encoding='utf-8'))['documents']}
  self.assertTrue(r['matches'])
  for match in r['matches']:
   doc=docs[match['id']]
   self.assertEqual(doc['status'],'ACTIVE');self.assertTrue(match['sources'])
   for key in ('sources','updated_at','authority'):self.assertEqual(match[key],doc[key])
   self.assertGreaterEqual(match['updated_at'],'2026-10-16')
  pricing=r['guides']['pricing']
  for rule in ('贡献利润除以买家售价必须严格超过10%','精确10%不通过','不自动切换为净回款10%','采购、国内运费、包装条件及预留缺失不假设0','发布与线上改价另行确认'):
   self.assertIn(rule,pricing)
  self.assertEqual(docs['pricing']['authority'],'USER_POLICY')
 def test_no_unrelated_answer_or_expired_reference(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'knowledge/project/knowledge.json';p.parent.mkdir(parents=True)
   p.write_text(json.dumps({'documents':[{'id':'old','status':'ACTIVE','authority':'OFFICIAL_REFERENCE','valid_until':'2020-01-01','title':'包裹','keywords':'尺寸','content':'obsolete'}]}),encoding='utf-8')
   self.assertEqual(retrieve(folder,'包裹尺寸')['retrieval_status'],'NO_RELEVANT_EVIDENCE')
 def test_multi_sku_and_images_route_to_correct_guides(self):
  self.assertIn('multi_sku',retrieve(ROOT,'多SKU为什么缺站点和不同规格图片')['guides'])
  self.assertIn('images',retrieve(ROOT,'商品图片如何生成主图及三张可用图库')['guides'])
 def test_full_sku_diagnostics_do_not_lose_required_fields(self):
  from src.sku_diagnostics_v3 import summarize,diagnostic_answer
  sku={'sku_id':'a','site_pricing':[{'site_id':'MLM','net_proceeds':5}], 'package':{'weight_g':100},'ai_processing':{'status':'done'},'ai_content':{'attributes':[],'attribute_completion':{'required_ids':['BRAND','MATERIAL','PACKAGE_LENGTH']}}}
  d={'evidence':{'sku_details':[sku,{**copy.deepcopy(sku),'sku_id':'b'}],'image_review':{'source_gallery':[{'remote_url':'one'}]}}}
  r=summarize(d);counts={b['label']:b['sku_count'] for b in r['blockers']}
  self.assertEqual(counts['品牌依据缺失'],2);self.assertEqual(counts['材质依据缺失'],2);self.assertEqual(counts['包装尺寸缺失'],2)
  self.assertEqual(r['content_stage_done'],2);self.assertEqual(r['vision_screened_count'],0)
  text=diagnostic_answer('Banner',r);self.assertIn('重新采集该SKU',text);self.assertIn('材质',text);self.assertIn('不等于供应商没有',text)
 def test_buyer_price_survives_privacy_filter_but_personal_fields_do_not(self):
  from src.workbench_assistant import clean
  self.assertEqual(clean({'buyer_price_usd':'10','buyer_id':'secret','api_key':'secret'}),{'buyer_price_usd':'10'})
 def test_selected_amount_question_is_not_misrouted_to_general_knowledge(self):
  from src.workbench_assistant import knowledge_question
  self.assertFalse(knowledge_question('这件商品采购价8元利润怎么算',{'product':'selected'}))
  self.assertTrue(knowledge_question('利润率是什么意思',{'product':'selected'}))
 def test_knowledge_has_one_copy_of_content_and_can_fall_back(self):
  from unittest.mock import patch
  with patch('src.rag_runtime_v3.search',return_value=None):r=retrieve(ROOT,'商品图片图库')
  self.assertFalse(r['semantic_available']);self.assertTrue(r['guides']);self.assertTrue(all('content' not in m for m in r['matches']))
 def test_brand_material_model_need_matching_supplier_evidence(self):
  from src.attribute_context_v3 import source_value_supported
  self.assertFalse(source_value_supported({'id':'BRAND','value_name':'Generic'},{'attributes':{}}))
  self.assertFalse(source_value_supported({'id':'MATERIAL','value_name':'Steel'},{'attributes':{'材质':'塑料'}}))
  self.assertFalse(source_value_supported({'id':'MATERIAL','value_name':'Metal'},{'attributes':{'材质':'非金属'}}))
  self.assertFalse(source_value_supported({'id':'MODEL','value_name':'03'},{'attributes':{'货号':'03'}}))
  self.assertTrue(source_value_supported({'id':'BRAND','value_name':'Generic'},{'attributes':{'品牌':'无品牌'}}))
  self.assertTrue(source_value_supported({'id':'MATERIAL','value_name':'Polyester'},{'attributes':{'材质':'涤纶'}}))
if __name__=='__main__':unittest.main()
