import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.listing_brand_v3 import apply, issues
from src.attribute_context_v3 import source_value_supported
from src.round2_evidence_v3 import candidate_attributes, recover
from src.publish_service import build_user_product_payload, PublishError


class StrictGenericTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'config').mkdir()
        (self.root/'config/attribute_policy.json').write_text(json.dumps({
            'version':'strict-test','origin':'USER_BRAND_POLICY',
            'brand_default':'Generic','brand_mode':'STRICT_GENERIC'}),encoding='utf-8')

    def draft(self):
        return {'human_approved':True,'payload':{'title':'FervorFox Manual Saw',
            'attributes':[{'id':'BRAND','value_id':'old-brand','value_name':'FervorFox'}]},
            'evidence':{'supplier_page':{'title':'FervorFox source', 'attributes':{'品牌':'FervorFox','型号':'ABC'}},
                'sku_details':[{'sku_id':'s','purchase_cost_cny':9.35,'package':{'weight_g':113},
                    'supplier_facts':{'attributes':{'品牌':'FervorFox'}},
                    'ai_content':{'title':'FervorFox Manual Saw','description':'For use with Stihl saws.',
                        'attributes':[{'id':'BRAND','value_name':'FervorFox'},{'id':'MODEL','value_name':'ABC'}]}}]}}

    def test_overrides_listing_brand_preserves_supplier_facts_costs_and_compatibility(self):
        d=self.draft();source=copy.deepcopy(d['evidence']['supplier_page']);s=copy.deepcopy(d['evidence']['sku_details'][0])
        self.assertTrue(apply(d,self.root));self.assertFalse(d['human_approved']);self.assertFalse(issues(d))
        self.assertEqual(d['payload']['title'],'Generic Manual Saw')
        self.assertEqual(d['payload']['attributes'],[{'id':'BRAND','value_id':None,'value_name':'Generic'}])
        current=d['evidence']['sku_details'][0]
        for key in ['package','purchase_cost_cny','supplier_facts']:
            self.assertEqual(s[key],current[key])
        self.assertEqual(d['evidence']['supplier_page'],source)
        self.assertEqual(current['ai_content']['description'],'For use with Stihl saws.')
        self.assertFalse(apply(d,self.root))

    def test_source_brand_cannot_override_strict_policy(self):
        d=self.draft();apply(d,self.root);sku=d['evidence']['sku_details'][0]
        supplier={**d['evidence']['supplier_page'],'user_attribute_policy':sku['user_attribute_policy']}
        definition={'id':'BRAND','value_type':'string'}
        self.assertEqual(candidate_attributes(supplier,[definition]),[{'id':'BRAND','value_id':None,'value_name':'Generic'}])
        self.assertTrue(source_value_supported({'id':'BRAND','value_name':'Generic'},supplier,definition))
        self.assertFalse(source_value_supported({'id':'BRAND','value_name':'FervorFox'},supplier,definition))

    def test_payload_builder_refuses_later_manual_or_model_reversion(self):
        d=self.draft();apply(d,self.root)
        next(a for a in d['evidence']['sku_details'][0]['ai_content']['attributes'] if a['id']=='BRAND')['value_name']='Wrong Brand'
        self.assertTrue(issues(d))
        with self.assertRaisesRegex(PublishError,'Generic'):build_user_product_payload(d,[])

    def test_without_strict_configuration_legacy_behavior_is_unchanged(self):
        (self.root/'config/attribute_policy.json').write_text('{}',encoding='utf-8')
        d=self.draft();before=copy.deepcopy(d)
        self.assertFalse(apply(d,self.root));self.assertEqual(d,before);self.assertFalse(issues(d))


if __name__=='__main__':unittest.main()
