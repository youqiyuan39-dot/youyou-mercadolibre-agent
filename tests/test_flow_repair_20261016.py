import copy,json,tempfile,time,types,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from unittest.mock import patch
from src import market_follow_v3 as follow,autopricing_v3 as pricing,auto_processing_v3 as queue
from src.source_intake import normalize_collection,build_processing_draft,refresh_draft_from_collection
from tests.test_source_intake import SourceIntakeTests
from tests.test_autopricing_v3 import draft

def ref(**changes):
    return {'market_item_id':'MLM3512099265','site_id':'MLM','fulfillment':'remote','buyer_price_usd':'5.68','net_proceeds_usd':'4.60','observed_at':datetime.now(timezone.utc).isoformat(),'spec_match':'HUMAN_CONFIRMED_SAME_SKU',**changes}

class MarketFloorTests(unittest.TestCase):
    def solve(self,gross=100,net=80,cost=70):
        p={**pricing.DEFAULT_POLICY,'minimum_margin_pct':'10','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'BUYER_PRICE','ads_net_pct':'0','tax_reserve_net_pct':'0','returns_base_pct':'0'}
        return follow.solve(gross,net,cost,0,0,0,1,p)
    def test_exact_ten_percent_passes_without_raising_price(self):
        r=self.solve();self.assertEqual(r['sales_margin_pct'],'10.00');self.assertTrue(r['floor_passed']);self.assertEqual(r['buyer_price_usd'],'100.00')
    def test_net_margin_is_not_sales_margin(self):
        r=self.solve(net=50,cost=44);self.assertEqual(r['margin_pct'],'12.00');self.assertEqual(r['sales_margin_pct'],'6.00');self.assertFalse(r['floor_passed'])
    def test_negative_profit_not_clamped_or_price_raised(self):
        r=self.solve(cost=90);self.assertEqual(r['unit_contribution_usd'],'-10.00');self.assertFalse(r['floor_passed']);self.assertEqual(r['net_proceeds_usd'],'80.00')
    def test_rounding_cannot_make_under_floor_pass(self):
        r=self.solve(cost='70.004');self.assertEqual(r['sales_margin_pct'],'10.00');self.assertFalse(r['floor_passed'])
    def test_unknown_cost_stays_unknown(self):
        with self.assertRaises(ValueError):self.solve(cost=None)
    def test_reference_rejects_full_old_and_wrong_site(self):
        for changes in ({'fulfillment':'full'},{'site_id':'MLB'},{'observed_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()},{'net_proceeds_usd':100}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):follow.reference(ref(**changes))
    def test_other_sku_cannot_borrow_selected_package(self):
        d=draft();d['evidence']['packed_weight']={'value':650};d['evidence']['packed_dimensions']={'length':26,'width':8,'height':4};d['evidence']['sku_details'][1]['package']={}
        with self.assertRaises(ValueError):pricing.forwarder(d['evidence']['sku_details'][1],d,pricing.DEFAULT_POLICY)

class IntakeRepairTests(unittest.TestCase):
    def raw(self):
        data=SourceIntakeTests();data.setUp();return copy.deepcopy(data.payload)
    def test_market_price_and_category_follow_single_source_sku(self):
        raw=self.raw();raw['market_reference']=ref(category_path='Herramientas > Bolsas Portaherramientas')
        data=normalize_collection(raw);d=build_processing_draft(data,Path('intake.json'),Path('.'))
        self.assertEqual(d['payload']['sites_to_sell'][0]['site_id'],'MLM');self.assertEqual(d['evidence']['sku_details'][0]['market_reference']['buyer_price_usd'],'5.68')
    def test_multi_sku_does_not_share_one_market_reference(self):
        raw=self.raw();raw['market_reference']=ref();raw['skus'].append({'sku_id':'second','variant':'other'})
        data=normalize_collection(raw);d=build_processing_draft(data,Path('intake.json'),Path('.'))
        self.assertNotIn('market_reference',d['evidence']['sku_details'][1])
    def test_recollection_fills_missing_sku_package_and_cost(self):
        raw=self.raw();raw['skus'][0]['package']={};raw['skus'][0]['price']=None
        old=normalize_collection(raw)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);queue.write(root/'old.json',old);d=build_processing_draft(old,root/'old.json',root)
            raw['skus'][0]['package']={'weight_g':200,'length_cm':11,'width_cm':14,'height_cm':3};raw['skus'][0]['price']=8
            fresh=normalize_collection(raw);new=refresh_draft_from_collection(d,fresh,root/'new.json',root)
            s=new['evidence']['sku_details'][0];self.assertEqual(s['package']['height_cm'],3);self.assertEqual(s['purchase_cost_cny'],8)
    def test_recollection_preserves_human_package(self):
        raw=self.raw();data=normalize_collection(raw)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);d=build_processing_draft(data,root/'old.json',root);s=d['evidence']['sku_details'][0];s['package'].update(status='USER_CONFIRMED',weight_g=999)
            raw['skus'][0]['package']={'weight_g':200};new=refresh_draft_from_collection(d,normalize_collection(raw),root/'new.json',root)
            self.assertEqual(new['evidence']['sku_details'][0]['package']['weight_g'],999)
    def test_nonfinite_numeric_source_is_rejected(self):
        raw=self.raw();raw['package']['weight_g']=float('nan');self.assertIsNone(normalize_collection(raw)['package']['weight_g'])

class RecoveryTests(unittest.TestCase):
    def test_owned_running_task_recovers_once_and_old_drafts_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);entry=root/'data/v3/auto-processing/owned-draft.json';job=root/'jobs/owned-draft-full-ai-job.json'
            queue.write(entry,{'draft':'owned-draft.json','status':'running'});queue.write(job,{'status':'running'})
            queue.write(root/'drafts/old-draft.json',{'untouched':True})
            queue.recover_interrupted(root);self.assertEqual(json.loads(entry.read_text())['status'],'pending');self.assertFalse((root/'data/v3/auto-processing/old-draft.json').exists())
            e=json.loads(entry.read_text());e['status']='running';queue.write(entry,e);queue.write(job,{'status':'running'});queue.recover_interrupted(root)
            self.assertEqual(json.loads(entry.read_text())['status'],'needs_attention')
    def test_explicit_user_pause_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);entry=root/'data/v3/auto-processing/owned-draft.json';job=root/'jobs/owned-draft-full-ai-job.json'
            queue.write(entry,{'draft':'owned-draft.json','status':'running'});queue.write(job,{'status':'paused'})
            queue.recover_interrupted(root);self.assertEqual(json.loads(job.read_text())['status'],'paused')

if __name__=='__main__':unittest.main()

class FullFollowTests(unittest.TestCase):
    def test_follow_plan_apply_and_legacy_cost_recalculation_agree(self):
        from src import pricing_tools_v3 as tools
        from src.pricing_intelligence import forwarder_unit_cost
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);d=draft();d['payload']['sites_to_sell']=[{'site_id':'MLM','logistic_type':'remote','net_proceeds':None,'price':None}]
            d['evidence']['sku_details']=d['evidence']['sku_details'][:1];s=d['evidence']['sku_details'][0]
            s.update(purchase_cost_cny=20,market_reference=ref(buyer_price_usd='20',net_proceeds_usd='16'))
            s['package']['weight_g']=6000
            p={**pricing.DEFAULT_POLICY,'pricing_strategy':'FOLLOW_MARKET','minimum_margin_pct':'10','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'BUYER_PRICE'}
            queue.write(root/'config/autopricing_policy.json',p);path=root/'drafts/demo-draft.json';queue.write(path,d)
            with patch.object(tools.operations,'store',return_value={'id':'s'}),patch.object(tools,'rate_quote',return_value={'cny_per_currency':'7','source':{'type':'FIXTURE'}}),patch.object(tools,'compare',side_effect=AssertionError('unnecessary market query')):
                plan=pricing.prepare(root,{'name':path.name,'store_id':'s','site_id':'MLM'})
                self.assertEqual(plan['state'],'READY_ESTIMATED');self.assertEqual(plan['rows'][0]['buyer_price_usd'],'20.00')
                self.assertEqual(path.read_text(encoding='utf-8'),json.dumps(d,ensure_ascii=False,indent=2))
                pricing.apply(root,plan['id'],True)
                result=json.loads(path.read_text(encoding='utf-8'));self.assertEqual(result['payload']['sites_to_sell'][0]['net_proceeds'],16)
                self.assertEqual(forwarder_unit_cost(result['pricing_plan'])['total_cny'],1.6)
                self.assertEqual(result['pricing_plan']['purchase_cost_cny'],20)
    def test_local_tab_context_inherits_without_last_product_fallback(self):
        from src.market_context_v3 import handle,resolve
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);handle(root,{'tab_id':10,'market_reference':ref()})
            self.assertEqual(handle(root,{'operation':'inherit','tab_id':20,'parent_tab_id':10})['market_reference']['market_item_id'],'MLM3512099265')
            self.assertIsNone(resolve(root,999))
            handle(root,{'tab_id':10,'market_reference':ref(market_item_id='MLM6258946880')})
            self.assertEqual(resolve(root,20)['market_item_id'],'MLM3512099265')
    def test_packaging_labels_do_not_accept_bare_dimensions(self):
        from src.package_recovery_v3 import extract
        self.assertEqual(extract('产品尺寸 11×14×3cm，净重80g'),{})
        self.assertEqual(extract('包装尺寸:110×140×30mm 包装重量:0.2kg'),{'length_cm':11,'width_cm':14,'height_cm':3,'weight_g':200})
        self.assertEqual(extract('包装尺寸:11×14×3cm 包装尺寸:12×14×3cm'),{})
    def test_local_ocr_recovers_only_matching_explicit_printed_values(self):
        from src.package_recovery_v3 import recover
        d={'evidence':{'supplier_page':{},'image_review':{'detail_gallery':[{'remote_url':'https://example.test/package.jpg'}]}}};sku={'sku_id':'one','package':{}}
        data={'confidence':'high','weight_g':200,'length_cm':11,'width_cm':14,'height_cm':3,'evidence':['PACKAGE SIZE:11 x14 x3 cm; PACKED WEIGHT:200g']}
        recover(Path('.'),d,[sku],inspector=lambda *args:data)
        self.assertEqual(sku['package']['weight_g'],200);self.assertEqual(sku['package']['height_cm'],3)
        d['evidence']['image_review']['detail_gallery']=[{'remote_url':'https://example.test/package.jpg'}];sku={'sku_id':'two','package':{}}
        recover(Path('.'),d,[sku],inspector=lambda *args:{**data,'length_cm':999})
        self.assertNotIn('length_cm',sku['package'])
