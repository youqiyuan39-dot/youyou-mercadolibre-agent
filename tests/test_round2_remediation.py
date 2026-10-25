import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from src.round2_evidence_v3 import recover,candidate_attributes
from src.source_image_screening_v3 import screen
from src.sku_processing_v3 import scope
from src import autopricing_v3 as pricing

class Round2Tests(unittest.TestCase):
    def test_banner_table_weight_style_and_pair_are_not_pennant_attributes(self):
        from src.attribute_context_v3 import source_value_supported,valid_value
        supplier={'attributes':{'颜色':'重量(g)','15':'30cm*180cm 100'},'selected_variant':'15 / 30cm*180cm'}
        for key,value in [('PENNANTS_NUMBER','100'),('PENNANT_WIDTH','15cm'),('PENNANT_LENGTH','30cm'),('TOTAL_LENGTH','180cm'),('IS_SUITABLE_FOR_OUTDOORS','No')]:
            self.assertFalse(source_value_supported({'id':key,'value_name':value},supplier))
        supplier['attributes']['旗片数量']='2'
        self.assertTrue(source_value_supported({'id':'PENNANTS_NUMBER','value_name':'2'},supplier))
        self.assertFalse(valid_value({'id':'TOTAL_LENGTH','value_name':'30cm*180cm 100'},{'value_type':'number_unit'}))
        self.assertTrue(valid_value({'id':'TOTAL_LENGTH','value_name':'180 cm'},{'value_type':'number_unit'}))
        from src.attribute_context_v3 import normalize_enum
        self.assertEqual(normalize_enum({'id':'PATTERN_NAME','value_id':'3426164','value_name':'骷髅花朵'},
            {'values':[{'id':'3426164','name':'Princesses'}]})['value_id'],None)

    def test_editor_preserves_complete_budget_and_invalidates_after_cost_change(self):
        from tests.test_autopricing_v3 import draft
        from src import integrated_pricing_v3 as integrated
        from src.draft_editor import create_edited_version
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);path=root/'drafts/demo-draft.json';d=draft()
            for s in d['evidence']['sku_details']:s['site_pricing']=[r for r in s['site_pricing'] if r['site_id']=='MLM']
            pricing.save(path,d)
            with patch.object(pricing,'policy',return_value={**pricing.DEFAULT_POLICY,'pricing_strategy':'FOLLOW_MARKET','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'NET_PROCEEDS'}),patch.object(pricing.prices.operations,'store',return_value={}),patch.object(integrated.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(pricing.prices,'rate_quote',return_value={'cny_per_currency':'7','source':{'type':'FIXED_TEST'}}):
                integrated.run(path,root)
                pricing.save(root/'config/autopricing_policy.json',pricing.policy(root))
            source=json.loads(path.read_text('utf-8'))
            p=create_edited_version(path.name,{'payload':{'description':'Human copy'}},root/'drafts',root/'versions')
            saved=json.loads(p.read_text('utf-8'))
            self.assertEqual(saved['pricing_plan']['profit_guidance'],source['pricing_plan']['profit_guidance'])
            self.assertEqual(integrated.summary(saved,root)['state'],'BUDGET_ESTIMATE')
            p=create_edited_version(path.name,{'pricing':{'purchase_cost_cny':99}},root/'drafts',root/'versions')
            saved=json.loads(p.read_text('utf-8'));self.assertEqual(integrated.summary(saved,root)['state'],'STALE')

    def test_other_brand_is_placeholder_but_explicit_supplier_brand_wins(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'config').mkdir();(root/'raw').mkdir()
            (root/'config/attribute_policy.json').write_text(json.dumps({'origin':'USER_BRAND_POLICY','brand_default':'general'}),'utf-8')
            for raw,expected in [('其他','general'),('璟途',None),('无品牌',None)]:
                (root/'raw/source.json').write_text(json.dumps({'attributes':{'品牌':raw}}),'utf-8')
                d={'evidence':{'source_capture':'raw/source.json','sku_details':[{'sku_id':'x','variant':'one'}]}}
                recover(root,d);policy=d['evidence']['sku_details'][0].get('user_attribute_policy') or {}
                self.assertEqual((policy.get('BRAND') or {}).get('value_name'),expected)
    def test_per_sku_human_edits_survive_scope_and_new_fact_metadata_survives_save(self):
        from tests.test_auto_sku_processing import fixture
        from src.draft_editor import create_edited_version
        from src.auto_processing_v3 import write
        d=fixture();d['evidence']['current_pricing_sku_id']='a'
        for s in d['evidence']['sku_details']:
            s['ai_content']={'description':'generated '+s['sku_id'],'attributes':[],'category_id':'CBT1'}
            s['product_measurements']={'dimensions_cm':[30,180],'dimension_type':'PRODUCT_OPTION_SIZE'}
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);write(root/'drafts/test-draft.json',d)
            path=create_edited_version('test-draft.json',{'payload':copy.deepcopy(d['payload']),'sku_content_edits':{'b':{'description':'User text B','attributes':[{'id':'BRAND','value_name':'User brand','value_id':None}]}}},root/'drafts',root/'draft_versions')
            saved=json.loads(path.read_text('utf-8'));b=saved['evidence']['sku_details'][1];work=scope(saved,b)
            self.assertEqual(work['payload']['description'],'User text B');self.assertTrue(work['edit_metadata']['field_locks']['description'])
            self.assertEqual(work['edit_metadata']['attribute_sources']['BRAND'],'manual');self.assertEqual(b['product_measurements']['dimensions_cm'],[30,180])

    def test_partial_pricing_saves_only_valid_sku_and_integration_undo_works(self):
        from tests.test_autopricing_v3 import draft
        from tests.test_flow_repair_20261016 import ref
        from src import integrated_pricing_v3 as integrated
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);d=draft();first,second=d['evidence']['sku_details'];first['market_reference']=ref(buyer_price_usd='40',net_proceeds_usd='35')
            for s in d['evidence']['sku_details']:s['site_pricing']=[r for r in s['site_pricing'] if r['site_id']=='MLM']
            d['payload']['sites_to_sell']=[r for r in d['payload']['sites_to_sell'] if r['site_id']=='MLM']
            path=root/'drafts/demo-draft.json';pricing.save(path,d);original=path.read_bytes()
            with patch.object(pricing,'policy',return_value={**pricing.DEFAULT_POLICY,'pricing_strategy':'FOLLOW_MARKET','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'BUYER_PRICE'}),patch.object(pricing.prices.operations,'store',return_value={}),patch.object(integrated.stores,'store_status',return_value={'active_store_id':'s'}),patch.object(pricing.prices,'rate_quote',return_value={'cny_per_currency':'7','source':{'type':'FIXED_TEST'}}):
                with self.assertRaises(ValueError):integrated.run(path,root)
                saved=json.loads(path.read_text('utf-8'));a,b=saved['evidence']['sku_details'];state=saved['evidence']['ai_processing_pricing']
                self.assertEqual(state['state'],'PARTIAL_BUDGET_ESTIMATE');self.assertEqual(a['site_pricing'][0]['net_proceeds'],35)
                self.assertEqual(b['site_pricing'][0]['net_proceeds'],second['site_pricing'][0]['net_proceeds'])
                pricing.undo(root,state['applied_plan_id'],True);self.assertEqual(json.loads(path.read_bytes()),json.loads(original))

    def test_exact_label_recovery_is_order_independent_and_not_common_size(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'raw').mkdir();capture={'title':'节日门帘','attributes':{},'skus':[], 'sku_image_evidence':[{'variant':'款式2','url':'https://cdn/b.jpg'},{'variant':'90x180cm','url':'https://cdn/ambiguous.jpg'}]}
            (root/'raw/c.json').write_text(json.dumps(capture),'utf-8')
            d={'evidence':{'source_capture':'raw/c.json','sku_details':[{'sku_id':'b','variant':'款式2 / 90x180cm'},{'sku_id':'a','variant':'款式1 / 90x180cm'}]}}
            recover(root,d);groups=d['evidence']['image_review']['sku_source_galleries']
            self.assertEqual([(g['sku_id'],g['gallery'][0]['remote_url']) for g in groups],[('b','https://cdn/b.jpg')])
            self.assertEqual(d['evidence']['sku_details'][0]['product_measurements']['dimensions_cm'],[90,180])
            self.assertNotIn('package',d['evidence']['sku_details'][0])

    def test_default_brand_and_official_material_enum_are_separate_from_source(self):
        definitions=[{'id':'BRAND','value_type':'string'},{'id':'MATERIAL','value_type':'string','values':[{'id':'42','name':'Plastic'}]}]
        supplier={'attributes':{'材质':'塑料'},'user_attribute_policy':{'BRAND':{'value_name':'general','origin':'USER_BRAND_POLICY'}}}
        before=copy.deepcopy(supplier);rows=candidate_attributes(supplier,definitions)
        self.assertEqual({r['id']:r['value_name'] for r in rows},{'BRAND':'general','MATERIAL':'Plastic'})
        self.assertEqual(next(r for r in rows if r['id']=='MATERIAL')['value_id'],'42');self.assertEqual(before,supplier)
        supplier['attributes']['品牌']='璟途';self.assertEqual(next(r for r in candidate_attributes(supplier,definitions) if r['id']=='BRAND')['value_name'],'璟途')

    def test_multisku_screen_clears_default_checkboxes_not_manual_selections(self):
        d={'evidence':{'sku_details':[{'sku_id':'a'},{'sku_id':'b'}], 'image_review':{'gallery':[{'remote_url':'https://x/poster'},{'remote_url':'https://x/manual','review_status':'HUMAN_SELECTED'}], 'source_gallery':[]}}}
        with tempfile.TemporaryDirectory() as t:screen(Path(t),d,d['evidence']['sku_details'],inspector=Mock())
        self.assertEqual([i['remote_url'] for i in d['evidence']['image_review']['gallery']],['https://x/manual'])

    def test_package_scan_includes_last_detail_panel_and_cache(self):
        from src.package_recovery_v3 import recover as packages
        d={'evidence':{'sku_details':[{'sku_id':'s','package':{}}], 'supplier_page':{},'image_review':{'detail_gallery':[{'remote_url':'https://x/'+str(n)} for n in range(14)]}}}
        inspect=Mock(side_effect=lambda root,image: {'confidence':'high','length_cm':20,'width_cm':10,'height_cm':2,'weight_g':None,'evidence':['包装尺寸:20×10×2cm']} if image['remote_url'].endswith('/13') else {'confidence':'low','evidence':[]})
        with tempfile.TemporaryDirectory() as t:
            packages(Path(t),d,d['evidence']['sku_details'],inspector=inspect);self.assertEqual(d['evidence']['sku_details'][0]['package']['height_cm'],2)
            count=inspect.call_count;packages(Path(t),d,d['evidence']['sku_details'],inspector=inspect);self.assertEqual(count,inspect.call_count)

    def test_follow_reference_failure_routes_budget_and_preserves_strategy(self):
        from tests.test_autopricing_v3 import draft
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'drafts').mkdir();(root/'drafts/demo-draft.json').write_text(json.dumps(draft()),'utf-8')
            p={**pricing.DEFAULT_POLICY,'pricing_strategy':'FOLLOW_MARKET','margin_policy_status':'CONFIRMED','confirmed_margin_basis':'NET_PROCEEDS'}
            with patch.object(pricing,'policy',return_value=p),patch.object(pricing.prices.operations,'store',return_value={}),patch.object(pricing.prices,'rate_quote',return_value={'cny_per_currency':'7','source':{'date':'2026-10-17'}}),patch.object(pricing.prices,'compare') as compare:
                r=pricing.prepare(root,{'name':'demo-draft.json','store_id':'s','query':'wrench','allow_cost_only':True})
                self.assertEqual(r['method'],'COST_BUDGET_REFERENCE_UNAVAILABLE');self.assertEqual(r['state'],'READY_ESTIMATED');self.assertFalse(r['buyer_margin_floor_verified']);compare.assert_not_called()
                pricing.apply(root,r['id'],True);saved=json.loads((root/'drafts/demo-draft.json').read_text('utf-8'));self.assertTrue(saved['evidence']['sku_details'][0]['site_pricing'][0]['net_proceeds'])
                pricing.undo(root,r['id'],True)
                d=draft();d['evidence']['sku_details'][0]['package']['height_cm']=None;(root/'drafts/demo-draft.json').write_text(json.dumps(d),'utf-8')
                r=pricing.prepare(root,{'name':'demo-draft.json','store_id':'s','query':'wrench','allow_cost_only':True});self.assertEqual(r['state'],'BLOCKED');self.assertIn('包装尺寸',r['errors'][0])
