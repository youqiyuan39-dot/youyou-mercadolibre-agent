import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src import market_follow_v3 as follow,autopricing_v3 as pricing,integrated_pricing_v3 as integrated
from src.task_evidence_v3 import restore
from tests.test_flow_repair_20261016 import ref

ROOT=Path(__file__).resolve().parents[1]
class ReferenceGuardTests(unittest.TestCase):
    def actual(self,offer):
        d=json.loads((ROOT/'docs/package-table-repair-20261017/before/drafts'/f'1688-{offer}-draft.json').read_text('utf-8'))
        restore(ROOT,d)
        # Time is a test clock; source product evidence and reference titles remain frozen.
        reference=d['evidence']['market_reference']
        reference['observed_at']=ref()['observed_at']
        d['evidence']['sku_details'][0]['market_reference']=copy.deepcopy(reference)
        return d
    @unittest.skipUnless((ROOT/'docs/package-table-repair-20261017/before/drafts').is_dir(), 'Private captured fixtures are not distributed')
    def test_actual_wrong_model_and_unconfirmed_pack_are_not_followed(self):
        for offer,code in [('800259756827','REFERENCE_SPEC_UNCONFIRMED'),('894875973731','REFERENCE_MODEL_CONFLICT')]:
            d=self.actual(offer);sku=d['evidence']['sku_details'][0]
            self.assertEqual(follow.reference_issue(d,sku,sku['market_reference'])['code'],code)
            with tempfile.TemporaryDirectory() as t:
                root=Path(t);pricing.save(root/'drafts/demo-draft.json',d)
                with patch.object(pricing,'policy',return_value={**pricing.DEFAULT_POLICY,'pricing_strategy':'FOLLOW_MARKET','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'NET_PROCEEDS'}),patch.object(pricing.prices.operations,'store',return_value={}),patch.object(pricing.prices,'compare',side_effect=AssertionError('no needless market query')),patch.object(pricing.prices,'rate_quote',return_value={'cny_per_currency':'7','source':{'type':'TEST'}}):
                    plan=pricing.prepare(root,{'name':'demo-draft.json','store_id':'s','site_id':'MLM','query':'filter','allow_cost_only':True})
                    self.assertEqual(plan['method'],'COST_BUDGET_REFERENCE_UNAVAILABLE');self.assertEqual(plan['state'],'READY_ESTIMATED')
                    self.assertFalse(plan['buyer_margin_floor_verified']);self.assertEqual(plan['reference_issues'][0]['code'],code)
                    self.assertNotIn('buyer_price_usd',plan['rows'][0])
                    pricing.apply(root,plan['id'],True)
                saved=json.loads((root/'drafts/demo-draft.json').read_text('utf-8'))
                self.assertNotEqual(saved['evidence']['sku_details'][0]['target_net_proceeds_usd'],12.67)
                self.assertEqual(saved['evidence']['sku_details'][0]['package'],sku['package'])
                self.assertEqual(saved['evidence']['sku_details'][0]['available_quantity'],200)
    @unittest.skipUnless((ROOT/'docs/package-table-repair-20261017/before/drafts').is_dir(), 'Private captured fixtures are not distributed')
    def test_explicit_confirmation_does_not_override_model_or_pack_conflict(self):
        d=self.actual('894875973731');s=d['evidence']['sku_details'][0];r={**s['market_reference'],'spec_match':'HUMAN_CONFIRMED_SAME_SKU'}
        self.assertEqual(follow.reference_issue(d,s,r)['code'],'REFERENCE_MODEL_CONFLICT')
        s['supplier_facts']['attributes']['每包数量']='1';r['title']='Paquete de 2 filtros VF5000'
        self.assertEqual(follow.reference_issue(d,s,r)['code'],'REFERENCE_PACK_CONFLICT')
        r['title']='1 piezas filtro VF5000'
        self.assertIsNone(follow.reference_issue(d,s,r))
    @unittest.skipUnless((ROOT/'docs/package-table-repair-20261017/before/drafts').is_dir(), 'Private captured fixtures are not distributed')
    def test_rule_version_invalidates_saved_budget(self):
        d=self.actual('800259756827');signature=integrated.signature(d)
        with patch.object(pricing,'VERSION','CHANGED_REFERENCE_RULE'):
            self.assertNotEqual(signature,integrated.signature(d))

if __name__=='__main__':unittest.main()
