import copy,json,tempfile,unittest
from pathlib import Path
from src.packaging_table_v3 import materialize
from src.source_intake import build_processing_draft,normalize_collection,refresh_draft_from_collection
from src.task_evidence_v3 import restore

ROOT=Path(__file__).resolve().parents[1]
class TableRecovery(unittest.TestCase):
 def sample(self):
  return {'offer_id':'123456789','title':'Filter','source_url':'https://detail.1688.com/offer/123456789.html','captured_at':'2026-10-17','pricing':{'amount':24},'package':{},'offer_flags':{'isSkuOffer':False},'skus':[],'package_rows':[{'package':{'weight_g':400,'length_cm':21.5,'width_cm':17.5,'height_cm':21.5}}]}
 @unittest.skipUnless((ROOT/'docs/package-table-repair-20261017/before/drafts').is_dir(), 'Private captured fixtures are not distributed')
 def test_actual_two_frozen_captures_restore_without_changing_source(self):
  for offer,expected in [('800259756827',(350,16.5,16.5,13.5)),('894875973731',(400,21.5,17.5,21.5))]:
   path=ROOT/'docs/package-table-repair-20261017/before/drafts'/f'1688-{offer}-draft.json';d=json.loads(path.read_text('utf-8'));capture=ROOT/d['evidence']['source_capture'];raw=capture.read_bytes();restore(ROOT,d);s=d['evidence']['sku_details'][0]
   self.assertEqual(tuple(s['package'][k] for k in ('weight_g','length_cm','width_cm','height_cm')),expected)
   self.assertEqual(s['sku_id'],'offer-'+offer);self.assertEqual(s['sku_id_origin'],'SINGLE_OFFER_WITHOUT_OPTIONS');self.assertEqual(s['available_quantity'],200)
   self.assertEqual(d['evidence']['packed_weight']['value'],expected[0]);self.assertEqual(capture.read_bytes(),raw)
   self.assertTrue(all(s['package']['field_sources'][k][0]['path'].startswith('/package_rows/') for k in ('weight_g','length_cm','width_cm','height_cm')))
 def test_materializer_is_pure_and_does_not_make_up_missing_source_skus(self):
  c=self.sample();old=copy.deepcopy(c);derived=materialize(c);self.assertEqual(c,old);self.assertEqual(len(derived['skus']),1)
  c['offer_flags']={};self.assertEqual(materialize(c)['skus'],[])
  c['offer_flags']={'isSkuOffer':True};self.assertEqual(materialize(c)['skus'],[])
 def test_multiple_skus_bind_by_id_not_row_order_or_shared_size(self):
  c=self.sample();c['skus']=[{'sku_id':'a','spec_id':'spec-a','variant':'red / 30x180cm'},{'sku_id':'b','spec_id':'spec-b','variant':'blue / 30x180cm'}]
  c['package_rows']=[{'sku_id':'b','package':{'weight_g':200}},{'spec_id':'spec-a','package':{'weight_g':100}},{'variant':'30x180cm','package':{'height_cm':9}}]
  d=materialize(c);self.assertEqual([s['package']['weight_g'] for s in d['skus']],[100,200]);self.assertTrue(all(s['package']['height_cm'] is None for s in d['skus']));self.assertEqual(len(d['package_table_resolution']['unassigned']),1)
 def test_product_dimensions_net_weight_and_conflicts_remain_unfilled(self):
  c=self.sample();c['package_rows'][0].update(dimension_type='PRODUCT',weight_type='NET_WEIGHT');d=materialize(c);self.assertFalse(any(d['skus'][0]['package'][k] for k in ('weight_g','length_cm','width_cm','height_cm')))
  c=self.sample();c['package_rows'].append({'package':{'weight_g':401}});d=materialize(c);self.assertIsNone(d['skus'][0]['package']['weight_g']);self.assertTrue(d['package_table_resolution']['conflicts'])
 def test_no_third_dimension_or_weight_guessing(self):
  c=self.sample();c['package_rows'][0]['package']={'length_cm':30,'width_cm':180};d=materialize(c);self.assertIsNone(d['skus'][0]['package']['height_cm']);self.assertIsNone(d['skus'][0]['package']['weight_g'])
 def test_manual_common_locks_survive_first_single_offer_recovery(self):
  with tempfile.TemporaryDirectory() as folder:
   r=Path(folder);p=r/'capture.json';c=self.sample();p.write_text(json.dumps(c),'utf-8');d={'payload':{},'evidence':{'source_capture':'capture.json','sku_details':[],'packed_weight':{'value':111,'unit':'g','status':'USER_CONFIRMED'}},'edit_metadata':{'field_locks':{'weight':True,'dimensions':True}}};before=copy.deepcopy(d['edit_metadata']);restore(r,d)
   self.assertEqual(d['evidence']['sku_details'][0]['package']['weight_g'],111);self.assertIsNone(d['evidence']['sku_details'][0]['package']['length_cm']);self.assertEqual(d['evidence']['packed_weight']['status'],'USER_CONFIRMED');self.assertEqual(d['edit_metadata'],before)
 def test_schema_keeps_fixed_table_headers_and_values(self):
  c=self.sample();c['package_rows'][0].update(headers=['长(cm)','宽(cm)','高(cm)','重量(g)'],values=['21.5','17.5','21.5','400'],table_title='包装信息 商品件重尺')
  d=normalize_collection(c);self.assertEqual(d['package_rows'][0]['headers'],c['package_rows'][0]['headers']);self.assertEqual(d['package_rows'][0]['values'],c['package_rows'][0]['values'])
 def test_recollection_updates_managed_package_and_preserves_locks(self):
  with tempfile.TemporaryDirectory() as folder:
   r=Path(folder);c=self.sample();d=build_processing_draft(c,r/'old.json',r);d['edit_metadata']={'field_locks':{'dimensions':True}};old=copy.deepcopy(d['evidence']['packed_dimensions']);c['package_rows'][0]['package'].update(weight_g=450,length_cm=25)
   result=refresh_draft_from_collection(d,c,r/'new.json',r);self.assertEqual(result['evidence']['sku_details'][0]['package']['weight_g'],450);self.assertEqual(result['evidence']['sku_details'][0]['package']['length_cm'],21.5);self.assertEqual(result['evidence']['packed_dimensions'],old)

if __name__=='__main__':unittest.main()
