import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from src.task_control_v3 import task,invoke,CandidateInvalid,BudgetExceeded
from src.task_evidence_v3 import restore,selected_fields,issues

class TaskHarnessTests(unittest.TestCase):
    def capture(self):
        from tests.test_source_intake import SourceIntakeTests
        from src.source_intake import normalize_collection
        t=SourceIntakeTests();t.setUp();raw=copy.deepcopy(t.payload)
        raw['collection_mode']='multi';raw['skus']=[{'sku_id':'a','variant':'14件套','price':10.3,'package':{'weight_g':45,'length_cm':20,'width_cm':23,'height_cm':2}}, {'sku_id':'b','variant':'15件套','price':None,'package':{'weight_g':100,'length_cm':20,'width_cm':22,'height_cm':2}}]
        raw['attributes']={'品牌':'品牌 无 无 无 适用 适用 油锯 油锯 油锯','产品规格':'类型 材质 价格 | 库存','14件套':'14 橡胶 ¥10.384158 ¥10.3 84158','15件套':'15 PA+30GF ¥17999 ¥17 999'}
        return normalize_collection(raw)
    def test_restores_unmaterialized_real_shape_without_cross_sku_cost(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data/intake').mkdir(parents=True);source=self.capture();(root/'data/intake/source.json').write_text(json.dumps(source),'utf-8')
            draft={'payload':{},'evidence':{'source_capture':'data/intake/source.json'}};restore(root,draft)
            a,b=draft['evidence']['sku_details'];self.assertEqual(a['package']['length_cm'],20);self.assertEqual(b['purchase_cost_cny'],17)
            self.assertEqual(a['supplier_facts']['attributes']['材质'],'橡胶');self.assertEqual(b['supplier_facts']['attributes']['材质'],'PA+30GF')
            self.assertEqual(a['supplier_facts']['attributes']['品牌'],'Generic')
    def test_confirmed_values_and_editor_empty_selection_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data/intake').mkdir(parents=True);(root/'data/intake/s.json').write_text(json.dumps(self.capture()),'utf-8')
            draft={'payload':{},'evidence':{'source_capture':'data/intake/s.json','sku_details':[{'sku_id':'a','variant':'14件套','purchase_cost_cny':99,'package':{'weight_g':650,'length_cm':30,'status':'USER_CONFIRMED'}}]}}
            restore(root,draft);self.assertEqual(draft['evidence']['sku_details'][0]['purchase_cost_cny'],99);self.assertEqual(draft['evidence']['sku_details'][0]['package']['weight_g'],650)
            draft={'payload':{},'evidence':{'source_capture':'data/intake/s.json'},'edit_metadata':{'sku_selection_confirmed':True}}
            restore(root,draft);self.assertNotIn('sku_details',draft['evidence'])
    def test_generated_candidate_does_not_fill_supplier_facts(self):
        s=self.capture();s['attributes']={};s['skus'][0]['ai_content']={'attributes':[{'id':'MATERIAL','value_name':'Titanium'}]}
        attrs,cost,quotes=selected_fields(s,s['skus'][0]);self.assertNotIn('材质',attrs)
    def test_missing_capture_does_not_prove_supplier_absence(self):
        d={'evidence':{'sku_details':[{'sku_id':'a','package':{}}]}}
        rows=issues(d);self.assertTrue(all(r['cause']=='CAPTURE_GAP' for r in rows));self.assertTrue(all(r['next_action']=='RECOLLECT_EXACT_SKU_PACKAGING' for r in rows))
    def test_schema_error_gets_exact_feedback_and_one_retry(self):
        error=CandidateInvalid('$.title is too long','bad output');call=Mock(side_effect=[error,{'provider':'local','paid_api_used':False,'usage':{},'output':[]}])
        with tempfile.TemporaryDirectory() as folder,task(Path(folder),'x','a'):
            invoke(Path(folder),{'input':'supplier text','text':{'format':{'name':'copy'}}},30,call)
        self.assertEqual(call.call_count,2);feedback=json.loads(call.call_args.args[1]['input'])['validation_feedback'];self.assertEqual(feedback['field_path'],'$.title');self.assertEqual(feedback['remaining_repairs'],0)
    def test_identical_invalid_output_and_transport_error_stop(self):
        error=CandidateInvalid('$.title invalid','same');call=Mock(side_effect=error)
        with tempfile.TemporaryDirectory() as folder,task(Path(folder),'x','a'):
            with self.assertRaises(CandidateInvalid):invoke(Path(folder),{'input':'facts'},30,call)
        self.assertEqual(call.call_count,2)
        call=Mock(side_effect=RuntimeError('HTTP 403'))
        with tempfile.TemporaryDirectory() as folder,task(Path(folder),'x','a'):
            with self.assertRaises(RuntimeError):invoke(Path(folder),{},30,call)
        self.assertEqual(call.call_count,1)
    def test_global_request_budget_stops_before_new_call(self):
        call=Mock(return_value={'usage':{},'output':[]})
        with tempfile.TemporaryDirectory() as folder,task(Path(folder),'x','a',request_limit=1):
            invoke(Path(folder),{},30,call)
            with self.assertRaises(BudgetExceeded):invoke(Path(folder),{},30,call)
        self.assertEqual(call.call_count,1)
    def test_image_rejection_must_name_a_concrete_failed_criterion(self):
        verdict={'usable_candidate':False,'reason':'scene not photo','watermark':False,'promotional_poster':False,'blurred':False,'mixed_variants':False}
        response={'provider':'local','paid_api_used':False,'usage':{},'output':[{'content':[{'text':json.dumps(verdict)}]}]}
        call=Mock(side_effect=[response,{**response,'output':[{'content':[{'text':json.dumps({**verdict,'usable_candidate':True,'reason':'clear secondary image'})}]}]}])
        with tempfile.TemporaryDirectory() as folder,task(Path(folder),'x',source_title='Printed door banner'):
            result=invoke(Path(folder),{'input':[],'text':{'format':{'name':'original_quality'}}},30,call)
        self.assertEqual(call.call_count,2);self.assertIn('clear secondary',str(result))
    def test_copy_cache_survives_downstream_attribute_change_but_not_source_change(self):
        from src.copy_artifact_v3 import signature,load
        draft={'payload':{},'evidence':{'source_title':'door banner','supplier_page':{'title':'door banner','attributes':{'品牌':'璟途'}}}}
        record={'copy_signature':signature(draft,'9b'),'provider':'local','paid_api_used':False,'proposal':{'title':'Banner'}}
        draft['payload']['attributes']=[{'id':'BRAND','value_name':'璟途'}]
        self.assertIsNotNone(load(record,draft,'9b',lambda *args:[]))
        draft['evidence']['supplier_page']['attributes']['品牌']='Other confirmed brand'
        self.assertIsNone(load(record,draft,'9b',lambda *args:[]))
    def test_exact_package_row_restored_without_neighbor_sku_bleed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data/intake').mkdir(parents=True);capture=self.capture()
            capture['skus'][0]['package']={};capture['skus'][1]['package']={}
            capture['package_rows']=[{'sku_id':'b','package':{'weight_g':44,'length_cm':3,'width_cm':4,'height_cm':5}}]
            (root/'data/intake/s.json').write_text(json.dumps(capture),'utf-8')
            draft={'payload':{},'evidence':{'source_capture':'data/intake/s.json'}};restore(root,draft)
            a,b=draft['evidence']['sku_details'];self.assertFalse(a['package'].get('length_cm'));self.assertEqual(b['package']['length_cm'],3)
    def test_unknown_required_attribute_is_missing_not_complete(self):
        d={'evidence':{'sku_details':[{'sku_id':'a','package':{'weight_g':1,'length_cm':1,'width_cm':1,'height_cm':1},'ai_content':{'attributes':[{'id':'MODEL','value_name':'Unknown'}],'attribute_completion':{'required_ids':['MODEL']}}}]}}
        self.assertEqual(issues(d)[0]['field'],'MODEL')
    def test_stale_materialized_supplier_fact_is_not_current_evidence(self):
        from src.sku_processing_v3 import scope
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data/intake').mkdir(parents=True);capture=self.capture()
            (root/'data/intake/s.json').write_text(json.dumps(capture),'utf-8')
            sku={'sku_id':'a','variant':'14件套','supplier_facts':{'origin':'SUPPLIER_CAPTURE','source_sha256':'stale','attributes':{'材质':'Titanium'}}}
            d={'payload':{},'evidence':{'source_capture':'data/intake/s.json','sku_details':[sku]}}
            self.assertNotEqual(scope(d,sku,root)['evidence']['supplier_page']['attributes'].get('材质'),'Titanium')
    def test_copy_feedback_catches_wrong_kit_type_and_does_not_pad(self):
        from src.ai_listing_pipeline import proposal_quality_issues
        d={'payload':{},'evidence':{'source_title':'化油器油泡油管','supplier_page':{'selected_variant':'14件套'}}}
        p={'title':'Fuel Cup Assembly Kit','description':'Confirmed product details:\n- Fourteen pieces.','key_facts':['Fourteen pieces.'],'methodology':{}}
        self.assertTrue(any('浮子杯' in x for x in proposal_quality_issues(p,d)))
        p['title']='Garden Tool Replacement Parts Kit'
        self.assertEqual(proposal_quality_issues(p,d),[])
    def test_optional_values_do_not_infer_personalization_or_pack_count(self):
        from src.attribute_context_v3 import source_value_supported,valid_value
        self.assertFalse(source_value_supported({'id':'IS_PERSONALIZED_PRODUCT','value_name':'No'},{'attributes':{'是否IP授权':'否'}}))
        self.assertFalse(source_value_supported({'id':'UNITS_PER_PACK','value_name':'1'},{'attributes':{}}))
        self.assertFalse(valid_value({'id':'COLOR','value_name':'颜色 90x180cm 是否跨境'},{'id':'COLOR','value_type':'string'}))
    def test_opaque_sku_suffix_cannot_become_chain_attribute_numbers(self):
        from src.attribute_context_v3 import source_value_supported
        supplier={'selected_variant':'3/8-050-063-2209导板头','attributes':{}}
        for key,value in [('GAUGE','3/8'),('PITCH','0.050'),('CHAIN_LINKS_NUMBER','2209'),('INCLUDES_CHAIN','No')]:
            self.assertFalse(source_value_supported({'id':key,'value_name':value},supplier))
        self.assertTrue(source_value_supported({'id':'LINKS_NUMBER','value_name':'37'},{'selected_variant':'6寸链条+导板37节','attributes':{}}))
        self.assertFalse(source_value_supported({'id':'LENGTH','value_name':'19.5'},{'attributes':{'包装长度':'19.5'}}))
    def test_copy_opaque_identifier_cannot_be_unpacked_to_physical_fields(self):
        from src.ai_listing_pipeline import proposal_quality_issues
        d={'payload':{},'evidence':{'source_title':'油锯导板头','supplier_page':{'selected_variant':'3/8-050-063-2209导板头'}}}
        p={'title':'Chainsaw Guide Bar Tip','description':'Confirmed product details:\n- A 3/8 pitch with 050 length.','methodology':{}}
        self.assertTrue(any('不可拆解' in x for x in proposal_quality_issues(p,d)))
    def test_link_count_cannot_be_assigned_to_guide_bar(self):
        from src.ai_listing_pipeline import proposal_quality_issues
        d={'payload':{},'evidence':{'source_title':'油锯链条','supplier_page':{'selected_variant':'6寸链条+导板37节'}}}
        p={'title':'Bar and Chain Kit','description':'Confirmed product details:\n- Guide bar link count is 37.','methodology':{}}
        self.assertTrue(any('导板没有链节数' in x for x in proposal_quality_issues(p,d)))
    def test_couplet_type_does_not_prove_quantity_in_selected_package(self):
        from src.ai_listing_pipeline import proposal_quality_issues
        d={'payload':{},'evidence':{'source_title':'亡灵节骷髅对联','supplier_page':{'selected_variant':'15 / 30cm*180cm'}}}
        p={'title':'Skull Banners','description':'Confirmed product details:\n- A pair of decorative banners.','methodology':{}}
        self.assertTrue(any('恰有两张' in x for x in proposal_quality_issues(p,d)))
if __name__=='__main__':unittest.main()
