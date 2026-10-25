"""Synthetic contracts only, never evidence of a real commercial match."""
import copy
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from src import procurement_v3 as p, product_match_v3 as match
from src import auto_processing_v3 as queue, integrated_pricing_v3 as pricing
from src.autopricing_v3 import forwarder


def fixture(mixed=False):
    raw = [{'sku_id': 'a', 'spec_id': 'spec-a', 'variant': 'A 单个装', 'price': '10', 'inventory': 100},
           {'sku_id': 'b', 'spec_id': 'spec-b', 'variant': 'B 单个装', 'price': '6', 'inventory': 100}]
    sku = {'sku_id': 'sale', 'variant': '混合2件装' if mixed else '单个装', 'purchase_cost_cny': 999,
           'package': {'weight_g': 200, 'length_cm': 11, 'width_cm': 14, 'height_cm': 3,
                       'status': 'USER_CONFIRMED', 'scope': 'SALES_UNIT'}, 'site_pricing': []}
    draft = {'payload': {}, 'evidence': {'supplier_url': 'https://detail.1688.com/offer/123456789.html',
              'sku_details': [sku], 'supplier_page': {'skus': raw}}}
    components = []
    for r in raw if mixed else raw[:1]:
        components.append({'source_offer_id': '123456789', 'source_sku_id': r['sku_id'], 'quantity': 1,
                           'units_per_purchase': 1, 'order_quantity': 10, 'minimum_order_quantity': 1,
                           'quantity_source': 'SYNTHETIC_TEST_QUOTE', 'source_signature': p.source_signature(draft, r),
                           'price_basis': {'currency': 'CNY', 'unit': '件', 'scope': 'PER_PROCUREMENT_UNIT',
                                           'source': 'SYNTHETIC_TEST_QUOTE', 'observed_at': '2026-10-18T16:00:00+08:00'}})
    sku['procurement'] = {'confirmation': 'HUMAN_CONFIRMED', 'confirmation_source': 'SYNTHETIC_TEST_ONLY',
                          'units_per_sale': len(components), 'components': components,
                          'freight_allocation': {'basis': 'PER_SALES_UNIT', 'source': 'SYNTHETIC_TEST_ALLOCATION'}}
    sku['package']['sales_unit_signature'] = p.plan_signature(sku['procurement'])
    return draft, sku


def resign(sku):
    sku['package']['sales_unit_signature'] = p.plan_signature(sku['procurement'])


class CompositionTests(unittest.TestCase):
    def test_changed_source_or_basis_structure_routes_to_input_not_crash(self):
        for changed in ['source','basis','freight','tiers']:
            d,s=fixture();raw=d['evidence']['supplier_page']['skus'][0]
            if changed=='source':d['evidence']['supplier_page']['skus']={'wrong':'shape'}
            if changed=='basis':s['procurement']['components'][0]['price_basis']='wrong'
            if changed=='freight':s['procurement']['freight_allocation']='wrong'
            if changed=='tiers':
                raw['pricing_tiers']={'wrong':'shape'};s['procurement']['components'][0]['source_signature']=p.source_signature(d,raw)
            resign(s)
            self.assertEqual(p.assess(d,s)['state'],'NEEDS_INPUT')

    def test_single_uses_source_not_legacy_amount(self):
        d, s = fixture(); before = copy.deepcopy(d)
        self.assertEqual(p.purchase_cost(d, s), '10')
        self.assertEqual(d, before)

    def test_same_sku_multiple_units(self):
        d, s = fixture(); plan = s['procurement']
        plan['components'][0]['quantity'] = 5; plan['units_per_sale'] = 5; resign(s)
        self.assertEqual(p.purchase_cost(d, s), '50')

    def test_mixed_goods_and_handling_distinct_components(self):
        d, s = fixture(True)
        self.assertEqual(p.purchase_cost(d, s), '16')
        self.assertEqual(str(forwarder(s, d, {'packing_service_cny': '0.6'})), '1.2')

    def test_quantity_or_quote_change_invalidates_package_and_price(self):
        d, s = fixture(); old = pricing.signature(d)
        d['evidence']['supplier_page']['skus'][0]['price'] = '11'
        self.assertNotEqual(pricing.signature(d), old)
        with self.assertRaisesRegex(ValueError, '报价已变化'):p.purchase_cost(d, s)
        self.assertEqual(s['purchase_cost_cny'], 999)

    def test_tier_quantity_moq_and_inventory(self):
        d, s = fixture(); raw = d['evidence']['supplier_page']['skus'][0]
        raw['pricing_tiers'] = [{'min_quantity': 1, 'price': '10'}, {'min_quantity': 10, 'price': '8'}]
        c = s['procurement']['components'][0];c['source_signature'] = p.source_signature(d, raw)
        c['price_basis']['tier_scope'] = 'PER_SKU';resign(s)
        self.assertEqual(p.purchase_cost(d, s), '8')
        c['minimum_order_quantity'] = 20;resign(s)
        with self.assertRaisesRegex(ValueError, '起订量'):p.purchase_cost(d, s)
        c['minimum_order_quantity'] = 1;raw['inventory'] = 0
        c['source_signature'] = p.source_signature(d, raw);resign(s)
        with self.assertRaisesRegex(ValueError, '库存'):p.purchase_cost(d, s)

    def test_no_cross_offer_or_duplicate_or_fractional_quantity(self):
        for key, value in [('source_offer_id', '987654321'), ('quantity', 0), ('quantity', 1.5), ('quantity', True), ('units_per_purchase', None)]:
            with self.subTest(key=key, value=value):
                d, s = fixture();s['procurement']['components'][0][key] = value;resign(s)
                with self.assertRaises(ValueError):p.purchase_cost(d, s)
        d, s = fixture();s['procurement']['components'] *= 2;resign(s)
        with self.assertRaisesRegex(ValueError, '重复'):p.purchase_cost(d, s)

    def test_components_need_same_order_batch_for_tier_and_freight(self):
        d, s = fixture(True);s['procurement']['components'][1]['order_quantity'] = 20;resign(s)
        with self.assertRaisesRegex(ValueError, '销售套数不一致'):p.purchase_cost(d, s)

    def test_packaging_cannot_be_borrowed_from_components(self):
        d, s = fixture();s['procurement']['components'][0]['quantity'] = 2;s['procurement']['units_per_sale'] = 2
        with self.assertRaisesRegex(ValueError, '实际包装'):p.purchase_cost(d, s)

    def test_unknown_freight_currency_units_and_confirmation_do_not_default(self):
        for action in [lambda x:x.pop('confirmation'), lambda x:x.pop('freight_allocation'),
                       lambda x:x['components'][0]['price_basis'].update(currency='USD'),
                       lambda x:x['components'][0]['price_basis'].pop('unit')]:
            d, s = fixture();action(s['procurement']);resign(s)
            self.assertEqual(p.assess(d, s)['state'], 'NEEDS_INPUT')

    def test_invalid_composition_blocks_even_without_market_reference(self):
        d, s = fixture();s['procurement'] = None
        self.assertEqual(match.assess(d, s)['state'], 'CANDIDATE')
        self.assertTrue(match.blocking_issues(d));self.assertTrue(p.image_scope_issues(d))

    def test_confirmed_multi_pack_is_not_confused_with_raw_single_item(self):
        d, s = fixture();s['procurement']['units_per_sale'] = 2;s['procurement']['components'][0]['quantity'] = 2;resign(s)
        d['evidence']['market_reference'] = {'market_item_id': 'MLM1234567', 'title': 'Paquete De 2',
                                            'spec_match': 'HUMAN_CONFIRMED_SAME_SKU'}
        self.assertEqual(match.assess(d, s)['state'], 'CONFIRMED')
        d['evidence']['market_reference']['title'] = 'Paquete De 3'
        self.assertEqual(match.assess(d, s)['state'], 'CONFLICT')

    def test_component_image_is_not_bundle_image(self):
        d, s = fixture(True)
        d['evidence']['image_review'] = {'sku_listing_galleries': [{'sku_id': 'sale', 'gallery': [{'local_file': 'component.jpg'}]}]}
        self.assertTrue(p.image_scope_issues(d))
        d['evidence']['image_review']['sku_listing_galleries'][0]['sales_unit_signature'] = p.plan_signature(s['procurement'])
        self.assertEqual(p.image_scope_issues(d), [])

    def test_scoped_copy_and_manual_save_keep_composition(self):
        from src.sku_processing_v3 import scope
        from src.draft_editor import create_edited_version
        d, s = fixture(True)
        scoped = scope(d, s)
        self.assertEqual(p.purchase_cost(scoped, scoped['evidence']['sku_details'][0]), '16')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp);queue.write(root/'drafts/a-draft.json', d)
            out = create_edited_version('a-draft.json', {}, root/'drafts', root/'draft_versions')
            saved = json.loads(out.read_text('utf-8'))
            self.assertEqual(saved['evidence']['sku_details'][0]['procurement'], s['procurement'])
            self.assertEqual(saved['evidence']['sku_details'][0]['package']['sales_unit_signature'], s['package']['sales_unit_signature'])


class QueueBusinessStateTests(unittest.TestCase):
    def test_same_missing_input_grouped_but_conflicting_counts_kept(self):
        issues=[{'sku_id':str(i),'issues':[{'code':'PACK','field':'units','reason':'confirm count','supplier_value':1,'reference_value':8}]} for i in range(11)]
        grouped=queue.input_requests(issues)
        self.assertEqual(len(grouped),1);self.assertEqual(len(grouped[0]['sku_ids']),11)
        issues.append({'sku_id':'different','issues':[{'code':'PACK','field':'units','reason':'confirm count','supplier_value':2,'reference_value':8}]})
        self.assertEqual(len(queue.input_requests(issues)),2)

    def test_transport_done_is_not_business_done_and_other_jobs_continue(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp);stopped = threading.Event()
            legacy = types.SimpleNamespace(start_full_ai_flow=Mock())
            for name, completion in [('a', 'NEEDS_MATCH'), ('b', 'READY_FOR_FINAL_REVIEW'), ('c', None)]:
                path = root/'drafts'/f'{name}-draft.json'
                queue.enqueue(root, path, {'title': name})
                entry = root/'data/v3/auto-processing'/f'{name}-draft.json'
                data = json.loads(entry.read_text('utf-8'));data['status'] = 'running';queue.write(entry, data)
                queue.write(root/'jobs'/f'{name}-draft-full-ai-job.json', {'status': 'done', 'completion_state': completion,
                            'items': [{'sku_id': name, 'status': 'blocked_identity'}], 'message': 'specific missing input'})
            bad = root/'data/v3/auto-processing/0-bad.json';bad.write_text('{', encoding='utf-8')
            queue.dispatch_once(root, legacy, stopped)
            entries = [json.loads((root/f'data/v3/auto-processing/{n}-draft.json').read_text('utf-8')) for n in ['a', 'b', 'c']]
            self.assertEqual([e['status'] for e in entries], ['needs_attention', 'done', 'needs_attention'])
            self.assertEqual(entries[0]['completion_state'], 'NEEDS_MATCH')
            self.assertEqual(entries[0]['items'][0]['sku_id'], 'a')
            queue.dispatch_once(root, legacy, stopped);legacy.start_full_ai_flow.assert_not_called()
            self.assertTrue(queue.enqueue(root, root/'drafts/a-draft.json', {'title': 'a'})['duplicate'])
            self.assertEqual(queue.enqueue(root, root/'drafts/a-draft.json', {'title': 'corrected'})['status'], 'pending')


if __name__ == '__main__':unittest.main()
