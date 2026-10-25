import unittest
from unittest.mock import patch
from src.barcode_policy import barcode_issues
from src.product_core_v3 import review_issues
from src.validate_listing import validate
from src.business_brief_v3 import action_for

class BarcodePolicyTests(unittest.TestCase):
    def category(self, tags):
        return {'category_id':'CBT123', 'query_source':'MERCADOLIBRE_OFFICIAL_API_LIVE', 'all_attributes':[{'id':'GTIN','tags':tags}]}
    def draft(self):
        return {'payload':{'category_id':'CBT123','condition':'new'}, 'evidence':{'gtin_or_exemption':'NO_GTIN_PENDING_CATEGORY_CHECK'}}
    def test_conditional_or_unknown_rule_is_quiet(self):
        self.assertEqual(barcode_issues(self.draft(),category=self.category(['conditional_required'])),[])
        self.assertEqual(barcode_issues(self.draft(),category={}),[])
    def test_explicit_requirement_is_visible(self):
        self.assertTrue(barcode_issues(self.draft(),category=self.category(['required'])))
        self.assertTrue(barcode_issues(self.draft(),category=self.category({'required':True})))
    def test_new_requirement_does_not_block_used_items(self):
        d=self.draft();self.assertTrue(barcode_issues(d,category=self.category(['new_required'])))
        d['payload']['condition']='used';self.assertEqual(barcode_issues(d,category=self.category(['new_required'])),[])
    def test_mismatched_or_unofficial_category_is_not_proof(self):
        c=self.category(['required']);c['category_id']='CBT456'
        self.assertEqual(barcode_issues(self.draft(),category=c),[])
        c=self.category(['required']);c['query_source']='AI'
        self.assertEqual(barcode_issues(self.draft(),category=c),[])
    def test_multi_sku_requires_corresponding_codes(self):
        d=self.draft();d['evidence'].update(gtin='common',sku_details=[{'sku_id':'A','gtin':'real-A'},{'sku_id':'B'}])
        issues=barcode_issues(d,category=self.category(['required']))
        self.assertEqual(len(issues),1);self.assertIn('SKU B',issues[0])
    def test_empty_marker_is_not_required_but_generated_code_stays_blocked(self):
        d=self.draft();self.assertFalse(any('条码' in v for v in review_issues(d)))
        d['payload']['attributes']=[{'id':'GTIN','value_name':'1234567890123','source':'auto_generated'}]
        self.assertTrue(any('条码' in v for v in review_issues(d)))
    def test_actual_platform_rejection_is_preserved_in_business_advice(self):
        error='GTIN required for this brand: item.attribute.missing_conditional_required'
        result=action_for({'stage':'PUBLISH_BLOCKED','publish_error':error,'in_publish_center':True})
        self.assertIn(error,result['issues']);self.assertNotIn('条码',result['next_step'])
    def test_validator_uses_explicit_rule_without_generic_no_code_warning(self):
        with patch('src.validate_listing.barcode_issues',return_value=['该类目明确要求商品条码（GTIN），当前商品未填写']):
            errors,_=validate(self.draft())
        self.assertTrue(any('明确要求' in v for v in errors))
