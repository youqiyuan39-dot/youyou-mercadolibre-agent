import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.model_resolution_v3 import load_rules, resolve, refresh, attach_context
from src.attribute_context_v3 import source_value_supported
from src.round2_evidence_v3 import candidate_attributes
from src.project_knowledge_v3 import retrieve


ROOT=Path(__file__).resolve().parents[1]
DEFINITION={'id':'MODEL','name':'Model','value_type':'string'}


class ModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.rules=load_rules(ROOT)

    def supplier(self, attributes, **extra):
        return {'attributes':attributes,'model_resolution_rules':copy.deepcopy(self.rules),**extra}

    def test_executable_knowledge_examples(self):
        examples=json.loads((ROOT/'knowledge/project/model_field_rules.json').read_text(encoding='utf-8'))['examples']
        for example in examples:
            with self.subTest(example=example['id']):
                result=resolve(self.supplier(example['input']))
                self.assertEqual(result['value_name'],example['expected'])
                self.assertEqual(result['status'],example['status'])

    def test_candidate_has_source_and_cannot_claim_manufacturer_confirmation(self):
        supplier=self.supplier({'货号':'11齿链锯','规格':'11齿链锯'})
        original=copy.deepcopy(supplier)
        result=resolve(supplier)
        self.assertEqual(result['sources'][0],{'label':'货号','quote':'11齿链锯','path':'/attributes/货号'})
        self.assertEqual(result['origin'],'DERIVED_LISTING_IDENTIFIER')
        self.assertFalse(result['manufacturer_model_confirmed'])
        self.assertTrue(result['requires_final_review'])
        self.assertEqual(candidate_attributes(supplier,[DEFINITION]),[{'id':'MODEL','value_id':None,'value_name':'11齿链锯'}])
        self.assertTrue(source_value_supported({'id':'MODEL','value_name':'11齿链锯'},supplier))
        self.assertFalse(source_value_supported({'id':'MODEL','value_name':'AI-NEW-11T'},supplier))
        self.assertEqual(supplier,original)

    def test_other_variant_cannot_supply_selected_sku_specification(self):
        supplier=self.supplier({'规格':'VF3500'},selected_variant='1个',model_source_sku_count=2)
        self.assertIsNone(resolve(supplier)['value_name'])
        supplier['model_selected_attributes']={'型号':'SELECTED-22'}
        self.assertEqual(resolve(supplier)['value_name'],'SELECTED-22')

    def test_disabled_candidate_policy_keeps_literal_model_only(self):
        self.assertIsNone(resolve({'attributes':{'货号':'ABC'}})['value_name'])
        self.assertEqual(resolve({'attributes':{'型号':'ABC'}})['value_name'],'ABC')

    def test_conflicting_explicit_models_do_not_fall_back_to_an_item_number(self):
        result=resolve(self.supplier({'型号':'A-1','Model':'B-2','货号':'C-3'}))
        self.assertEqual(result['status'],'CONFLICT')
        self.assertIsNone(result['value_name'])
        self.assertFalse(source_value_supported({'id':'MODEL','value_name':'A-1'},self.supplier({'型号':'A-1/B-2'})))

    def test_refresh_preserves_conflict_observations_and_raw_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'raw').mkdir();(root/'knowledge/project').mkdir(parents=True)
            (root/'knowledge/project/model_field_rules.json').write_text(json.dumps(self.rules),encoding='utf-8')
            raw={'title':'source','attributes':{'型号':'A-1','model':'B-2','货号':'C-3'},'skus':[{'sku_id':'s','variant':'1个'}]}
            path=root/'raw/source.json';path.write_text(json.dumps(raw),encoding='utf-8');before=path.read_bytes()
            draft={'evidence':{'source_capture':'raw/source.json','sku_details':[{'sku_id':'s','variant':'1个'}]}}
            refresh(root,draft);sku=draft['evidence']['sku_details'][0]
            self.assertEqual(sku['model_resolution']['status'],'CONFLICT')
            supplier={'attributes':{'货号':'C-3'}};attach_context(supplier,sku)
            self.assertEqual(resolve(supplier)['status'],'CONFLICT')
            self.assertEqual(path.read_bytes(),before)

    def test_generated_copy_and_knowledge_cases_are_not_product_facts(self):
        supplier=self.supplier({},title='Amazing Model VF3500',description='AI model ABC-9',model_resolution={'value_name':'VF3500'})
        self.assertIsNone(resolve(supplier)['value_name'])
        self.assertFalse(candidate_attributes(supplier,[DEFINITION]))

    def test_local_retrieval_returns_rule_and_current_brand_policy(self):
        result=retrieve(ROOT,'MODEL 型号 货号 候选',limit=3,semantic=False)
        self.assertIn('model_field_candidates',result['guides'])
        self.assertIn('候选不是厂家型号确认',result['guides']['model_field_candidates'])
        attrs=retrieve(ROOT,'类目属性 品牌 Generic',limit=3,semantic=False)
        self.assertIn('所有刊登品牌严格Generic',attrs['guides']['attributes'])


if __name__=='__main__':unittest.main()
