"""Synthetic response contracts, not live API/account verification."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.supplier_api_v3 import adapt_product_get, import_product_get
from src.source_intake import IntakeError, normalize_collection


def response():
    return {'productInfo': {'productID': 123456789, 'subject': 'SYNTHETIC TEST PRODUCT',
            'description': 'Synthetic source description',
            'attributes': [{'attributeName': '材质', 'value': '钢'}],
            'image': {'images': ['https://cbu01.alicdn.com/img/a.jpg']},
            'skuInfos': [{'skuId': 's1', 'specId': 'sp1', 'price': '12.50', 'amountOnSale': 20,
                          'attributes': [{'attributeName': '颜色', 'attributeValue': '黑',
                                          'skuImageUrl': 'https://cbu01.alicdn.com/img/a.jpg'}]},
                         {'skuId': 's2', 'specId': 'sp2', 'price': '13.50', 'amountOnSale': 0,
                          'attributes': [{'attributeName': '颜色', 'attributeValue': '白',
                                          'skuImageUrl': 'https://cbu01.alicdn.com/img/b.jpg'}]}],
            'shippingInfo': {'length': 10, 'width': 20, 'height': 30, 'unitWeight': 100},
            'intelligentInfo': {'title': 'algorithm title is not supplier fact'}}}


class ResponseTests(unittest.TestCase):
    def test_changed_api_envelope_is_rejected_without_silent_field_loss(self):
        for field,value in [('attributes',{'wrong':'shape'}),('image',['wrong'])]:
            r=response();r['productInfo'][field]=value
            with self.assertRaises(IntakeError):self.adapt(r)
        r=response();r['success']='false'
        with self.assertRaises(IntakeError):self.adapt(r)

    def adapt(self, value=None, **kw):
        return adapt_product_get(value or response(), '123456789', observed_at='2026-10-18T16:00:00+08:00', **kw)

    def test_explicit_sku_mapping_survives_global_duplicate(self):
        d = self.adapt();self.assertEqual(d['audit']['bound_image_groups'], 2)
        self.assertEqual([r['sku_id'] for r in d['capture']['sku_image_sets']], ['s1', 's2'])
        self.assertEqual(d['capture']['sku_image_sets'][0]['images'], d['capture']['images'])
        self.assertEqual(d['capture']['skus'][1]['inventory'], 0)

    def test_unknown_price_basis_and_product_shipping_not_promoted(self):
        d = self.adapt();self.assertEqual(d['audit']['priced_sku_count'], 0)
        self.assertTrue(all(s['package'] is None for s in d['capture']['skus']))
        self.assertIsNotNone(d['capture']['api_evidence']['shippingInfo'])
        self.assertNotEqual(d['capture']['title'], 'algorithm title is not supplier fact')
        self.assertEqual(normalize_collection(d['capture'])['api_evidence'], d['capture']['api_evidence'])

    def test_price_requires_real_origin_confirmed_currency_and_unit(self):
        basis = {'confirmed': True, 'currency': 'CNY', 'unit': '件', 'scope': 'PER_PROCUREMENT_UNIT', 'source': 'SYNTHETIC_BASIS'}
        self.assertEqual(self.adapt(quote_basis=basis)['audit']['priced_sku_count'], 0)
        self.assertEqual(self.adapt(evidence_kind='LIVE_OFFICIAL_API', quote_basis=basis)['audit']['priced_sku_count'], 2)
        basis['currency'] = 'USD'
        self.assertEqual(self.adapt(evidence_kind='LIVE_OFFICIAL_API', quote_basis=basis)['audit']['priced_sku_count'], 0)

    def test_tier_quote_not_replaced_by_lowest_or_other_price(self):
        r = response();r['productInfo']['skuInfos'][0].update(priceRange=[{'startQuantity': 10, 'price': '8'}], retailPrice='5')
        basis = {'confirmed': True, 'currency': 'CNY', 'unit': '件', 'scope': 'PER_PROCUREMENT_UNIT', 'source': 'SYNTHETIC_BASIS'}
        d = self.adapt(r, evidence_kind='LIVE_OFFICIAL_API', quote_basis=basis)
        self.assertIsNone(d['capture']['skus'][0]['price'])

    def test_duplicates_wrong_offer_and_unknown_wrapper_rejected(self):
        r = response();r['productInfo']['skuInfos'][1]['skuId'] = 's1'
        with self.assertRaisesRegex(IntakeError, '重复'):self.adapt(r)
        r = response();r['productInfo']['productID'] = 987654321
        with self.assertRaisesRegex(IntakeError, '不一致'):self.adapt(r)
        with self.assertRaisesRegex(IntakeError, '封装'):self.adapt({'result': response()})

    def test_business_failure_never_counts_as_success(self):
        for r in [{'success': False, **response()}, {'error_code': '450', **response()}, {'errMsg': 'failed', **response()}]:
            with self.assertRaisesRegex(IntakeError, '报错'):self.adapt(r)

    def test_credentials_are_rejected_without_echo(self):
        r = response();r['access_token'] = 'SYNTHETIC_DO_NOT_ECHO'
        with self.assertRaises(IntakeError) as cm:self.adapt(r)
        self.assertNotIn('SYNTHETIC_DO_NOT_ECHO', str(cm.exception))
        with self.assertRaises(IntakeError):self.adapt(quote_basis={'client_secret': 'SYNTHETIC'})

    def test_ambiguous_or_relative_urls_not_guessed(self):
        r = response();r['productInfo']['image']['images'] = ['relative.jpg', 'http://insecure/a.jpg']
        r['productInfo']['skuInfos'][0]['attributes'][0]['skuImageUrl'] = 'https://a/x.jpg?signature=unknown'
        d = self.adapt(r);self.assertEqual(d['capture']['images'], [])
        self.assertEqual(d['audit']['bound_image_groups'], 1)

    def test_import_uses_existing_queue_preserves_provenance_and_deduplicates(self):
        # Explicit LIVE label here tests the adapter contract in an isolated root;
        # it does not assert that the synthetic response was fetched remotely.
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            with self.assertRaises(IntakeError):import_product_get(root, response(), '123456789', observed_at='test', confirmed=True)
            a = import_product_get(root, response(), '123456789', observed_at='2026-10-18T16:00:00+08:00',
                                   confirmed=True, evidence_kind='LIVE_OFFICIAL_API')
            capture = json.loads(a['intake']['capture'].read_text('utf-8')) if 'capture' in a['intake'] else None
            self.assertFalse(a['intake']['auto_processing']['duplicate'])
            b = import_product_get(root, response(), '123456789', observed_at='2026-10-18T16:01:00+08:00',
                                   confirmed=True, evidence_kind='LIVE_OFFICIAL_API')
            self.assertTrue(b['intake']['auto_processing']['duplicate'])
            files = list((root/'data/intake').glob('*.json'))
            self.assertTrue(any('api_evidence' in json.loads(p.read_text('utf-8')) for p in files))


if __name__ == '__main__':unittest.main()
