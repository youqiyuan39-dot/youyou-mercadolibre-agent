"""Local-only pricing candidates: no assumed margin permission or price writes."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src import autopricing_v3 as pricing,market_follow_v3 as follow,integrated_pricing_v3 as integrated
from tests.test_autopricing_v3 import draft,comparison
from src.pricing_intelligence import profit_methodology,forwarder_unit_cost

class StrategyPricing(unittest.TestCase):
    def setUp(self):
        (Path(__file__).resolve().parents[1]/'docs/strategy-loop-20261018').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'docs/strategy-loop-20261018')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'drafts/demo-draft.json';self.data=draft()
        for sku in self.data['evidence']['sku_details']:sku['site_pricing']=[s for s in sku['site_pricing'] if s['site_id']=='MLM']
        self.policy=copy.deepcopy(pricing.DEFAULT_POLICY)
        self.store=patch.object(pricing.prices.operations,'store',return_value={});self.store.start();self.addCleanup(self.store.stop)
        self.comp=patch.object(pricing.prices,'compare',return_value=comparison());self.comp.start();self.addCleanup(self.comp.stop)
        self.fx=patch.object(pricing.prices,'rate_quote',return_value={'cny_per_currency':'7','source':{'type':'FIXED_TEST'}});self.fx.start();self.addCleanup(self.fx.stop)
        self.active=patch.object(integrated.stores,'store_status',return_value={'active_store_id':'s'});self.active.start();self.addCleanup(self.active.stop)
        self.save()
    def save(self):
        pricing.save(self.path,self.data);pricing.save(self.root/'config/autopricing_policy.json',self.policy)
    def prepare(self):
        return pricing.prepare(self.root,{'name':self.path.name,'store_id':'s','query':'wrench','allow_cost_only':True})
    def confirmed(self):
        self.policy.update(margin_policy_status='CONFIRMED',confirmed_margin_basis='NET_PROCEEDS');self.save()
    def test_pending_cost_candidate_preserves_all_site_prices(self):
        original=self.path.read_bytes();p=self.prepare()
        self.assertEqual(p['state'],'MARGIN_POLICY_PENDING');self.assertEqual(len(p['rows']),2)
        self.assertTrue(p['rows'][0]['unit_contribution_usd']);self.assertEqual(original,self.path.read_bytes())
        with self.assertRaisesRegex(ValueError,'待用户确认'):pricing.apply(self.root,p['id'],True)
        self.assertEqual(original,self.path.read_bytes())
    def test_pending_follow_calculates_but_cannot_claim_floor_passed(self):
        result=follow.solve('10','8','10','1','.6','0','7',self.policy)
        self.assertIsNone(result['floor_passed']);self.assertIsNone(result['minimum_margin_pct'])
        self.assertEqual(result['decision'],'MARGIN_POLICY_PENDING')
        self.assertEqual(result['margin_policy']['proposed_minimum_margin_pct'],'10.00')
    def test_confirmed_basis_must_match_the_denominator(self):
        self.confirmed();result=follow.solve('10','8','10','1','.6','0','7',self.policy)
        self.assertIsNone(result['floor_passed'])
        self.policy['confirmed_margin_basis']='BUYER_PRICE'
        self.assertTrue(follow.solve('10','8','10','1','.6','0','7',self.policy)['floor_passed'])
    def test_sku_domestic_cost_is_not_replaced_by_selected_draft(self):
        self.data['evidence']['sku_details'][0]['domestic_shipping_cny']=0
        self.data['evidence']['sku_details'][1]['domestic_shipping_cny']=18;self.save()
        p=self.prepare();self.assertEqual([r['domestic_cny'] for r in p['rows']],['0.00','18.00'])
    def test_explicit_unknown_domestic_cost_never_falls_back_to_zero(self):
        self.data['evidence']['sku_details'][1]['domestic_shipping_cny']=None;self.save();p=self.prepare()
        self.assertEqual(len(p['rows']),1);self.assertIn('基础成本缺失',p['errors'][0])
    def test_manual_price_lock_survives_even_confirmed_policy(self):
        self.confirmed();self.data['evidence']['sku_details'][1]['human_field_locks']={'pricing':True};self.save()
        original=self.path.read_bytes();p=self.prepare();self.assertEqual(p['locked_sku_ids'],['two'])
        with self.assertRaisesRegex(ValueError,'人工锁定'):pricing.apply(self.root,p['id'],True)
        self.assertEqual(original,self.path.read_bytes())
    def test_global_price_lock_is_honored(self):
        self.confirmed();self.data['edit_metadata']={'field_locks':{'price':True}};self.save()
        self.assertEqual(self.prepare()['locked_sku_ids'],['one','two'])
    def test_pending_integrated_result_is_saved_and_readable_not_applied(self):
        sites=copy.deepcopy(self.data['payload']['sites_to_sell'])
        with self.assertRaisesRegex(ValueError,'待用户确认'):integrated.run(self.path,self.root)
        saved=json.loads(self.path.read_text('utf-8'));summary=integrated.summary(saved,self.root)
        self.assertEqual(summary['state'],'MARGIN_POLICY_PENDING');self.assertEqual(len(summary['rows']),2)
        self.assertEqual(saved['payload']['sites_to_sell'],sites)
        self.assertNotIn('autopricing',saved['evidence'])
    def test_changed_policy_invalidates_saved_candidate_even_same_data(self):
        with self.assertRaises(ValueError):integrated.run(self.path,self.root)
        saved=json.loads(self.path.read_text('utf-8'));self.policy['ads_net_pct']='8'
        pricing.save(self.root/'config/autopricing_policy.json',self.policy)
        self.assertEqual(integrated.summary(saved,self.root)['state'],'STALE')
    def test_confirmed_apply_and_guarded_undo_are_preserved(self):
        self.confirmed();before=self.path.read_bytes();p=self.prepare()
        self.assertEqual(p['state'],'READY_ESTIMATED');pricing.apply(self.root,p['id'],True)
        self.assertNotEqual(self.path.read_bytes(),before)
        pricing.undo(self.root,p['id'],True);self.assertEqual(json.loads(self.path.read_bytes()),json.loads(before))
    def test_old_ready_plan_cannot_escape_new_pending_policy(self):
        self.confirmed();p=self.prepare();self.policy['margin_policy_status']='PENDING_CONFIRMATION'
        pricing.save(self.root/'config/autopricing_policy.json',self.policy)
        with self.assertRaisesRegex(ValueError,'待用户确认'):pricing.apply(self.root,p['id'],True)
    def test_follow_break_even_recalculates_percentage_costs_at_break_even(self):
        p={**self.policy,'ads_net_pct':'10','tax_reserve_net_pct':'10','returns_base_pct':'0','other_cny':'0'}
        result=follow.solve('100','80','40','0','0','0','1',p)
        self.assertEqual(result['break_even_net_usd'],'50.00')
        with self.assertRaises(ValueError):follow.solve('100','80','40','0','0','0','1',{**p,'tax_reserve_net_pct':'90'})
    def test_legacy_unknown_package_never_implies_zero_overweight_or_total(self):
        r=forwarder_unit_cost({})
        self.assertIsNone(r['over_5kg_surcharge_cny']);self.assertIsNone(r['total_cny'])
        self.assertIn('package_weight_g',r['missing_inputs'])
    def test_legacy_other_unknown_is_not_silently_zero(self):
        p={'mode':'net_proceeds','purchase_cost_cny':28,'domestic_shipping_cny':7,'exchange_rate_cny_per_usd':7,'packaging_cost_usd':.5}
        result=profit_methodology(p)
        self.assertEqual(result['status'],'REAL_PROFIT_UNVERIFIED');self.assertIn('other_cost_usd',result['missing_inputs'])
        self.assertNotIn('current_target',result);self.assertNotIn('recommended_net_proceeds_range_usd',result)
    def test_explicit_zero_scenarios_can_be_calculated_but_never_called_acceptable(self):
        p={'mode':'net_proceeds','purchase_cost_cny':28,'domestic_shipping_cny':7,'exchange_rate_cny_per_usd':7,
           'packaging_cost_usd':.5,'target_net_proceeds_usd':9,'package_weight_g':200,'package_length_cm':11,
           'package_width_cm':14,'package_height_cm':3,'forwarder_packing_service_cny':.6,
           'forwarder_packaging_material_cny':0,'forwarder_value_added_cny':0,'forwarder_label_cny':0,
           'cost_assumptions':{'other_cost_usd':{'value':0,'reason':'isolated zero-additional-cost scenario'}},'scenario_margin_pcts':[10,30]}
        result=profit_methodology(p)
        self.assertEqual(result['status'],'COST_SCENARIO_UNVERIFIED');self.assertTrue(result['assumptions'])
        self.assertEqual(result['current_target']['assessment'],'COST_SCENARIO_UNVERIFIED')
        self.assertEqual(len(result['scenario_targets']),2);self.assertNotIn('acceptable_margin_range_pct',result)
    def test_matched_reference_keeps_scope_and_redirect_evidence(self):
        from tests.test_flow_repair_20261016 import ref
        value=ref(source_sku_id='one',required_identity_fields=['model'],resolved_item_id='MLM123456789',identity_evidence=[{'field':'model'}])
        normalized=follow.reference(value)
        self.assertEqual(normalized['source_sku_id'],'one');self.assertEqual(normalized['identity_evidence'],value['identity_evidence'])
        self.assertEqual(follow.reference_issue(self.data,self.data['evidence']['sku_details'][0],normalized)['code'],'REFERENCE_REDIRECT_CONFLICT')
    def test_expired_reference_can_recover_to_authorized_cost_candidate(self):
        from tests.test_flow_repair_20261016 import ref
        self.policy.update(pricing_strategy='FOLLOW_MARKET',fallback_on_missing_reference=True)
        self.data['evidence']['sku_details']=self.data['evidence']['sku_details'][:1]
        self.data['evidence']['sku_details'][0]['market_reference']=ref(observed_at='2020-01-01T00:00:00+00:00');self.save()
        before=self.path.read_bytes();p=self.prepare()
        self.assertEqual(p['method'],'COST_BUDGET_REFERENCE_UNAVAILABLE');self.assertTrue(p['rows'])
        self.assertEqual(p['reference_issues'][0]['code'],'REFERENCE_STALE');self.assertEqual(p['state'],'MARGIN_POLICY_PENDING')
        self.assertEqual(before,self.path.read_bytes())
    def test_confirmed_floor_has_no_upper_cap_and_does_not_round_up_a_failure(self):
        p={**self.policy,'margin_policy_status':'CONFIRMED','confirmed_margin_basis':'BUYER_PRICE','ads_net_pct':'0','tax_reserve_net_pct':'0','returns_base_pct':'0'}
        exact=follow.solve('100','80','70',0,0,0,1,p);under=follow.solve('100','80','70.004',0,0,0,1,p)
        self.assertEqual(exact['sales_margin_pct'],under['sales_margin_pct']);self.assertTrue(exact['floor_passed']);self.assertFalse(under['floor_passed'])
        high=follow.solve('100','80','5',0,0,0,1,p);self.assertTrue(high['floor_passed']);self.assertEqual(high['sales_margin_pct'],'75.00')
    def test_one_locked_sku_does_not_stop_confirmed_unlocked_sku(self):
        self.confirmed();self.data['evidence']['sku_details'][1]['human_field_locks']={'pricing':True};self.save()
        original=copy.deepcopy(self.data['evidence']['sku_details'][1]['site_pricing'])
        with self.assertRaises(ValueError):integrated.run(self.path,self.root)
        saved=json.loads(self.path.read_text('utf-8'));a,b=saved['evidence']['sku_details']
        self.assertEqual(saved['evidence']['ai_processing_pricing']['state'],'PARTIAL_BUDGET_ESTIMATE')
        self.assertEqual(b['site_pricing'],original);self.assertIn('autopricing_by_site',a)
    def test_human_edit_during_candidate_calculation_is_preserved_and_not_validated_as_old_result(self):
        prepare=pricing.prepare
        def editing(*args,**kwargs):
            plan=prepare(*args,**kwargs);d=json.loads(self.path.read_text('utf-8'))
            d['payload']['description']='Human text entered while calculating';d['evidence']['sku_details'][0]['purchase_cost_cny']=999
            pricing.save(self.path,d);return plan
        with patch.object(pricing,'prepare',side_effect=editing),self.assertRaisesRegex(ValueError,'计算期间商品已修改'):integrated.run(self.path,self.root)
        saved=json.loads(self.path.read_text('utf-8'))
        self.assertEqual(saved['payload']['description'],'Human text entered while calculating')
        self.assertEqual(saved['evidence']['sku_details'][0]['purchase_cost_cny'],999)
        self.assertEqual(saved['evidence']['ai_processing_pricing']['state'],'STALE')

if __name__=='__main__':unittest.main()
