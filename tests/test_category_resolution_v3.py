import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src.category_resolution_v3 import resolve,apply_category,semantic_issues,assessment,intent_for,VERSION

def category(cid,name,root,branch,domain):
    return {'id':cid,'category_id':cid,'name':name,'path_from_root':[{'id':'root','name':root},{'id':'branch','name':branch},{'id':cid,'name':name}],
        'children_categories':[],'settings':{'status':'enabled','listing_allowed':True,'catalog_domain':'CBT-'+domain}}
CAR=category('CBT412006','Keychains','Vehicle Accessories','Tuning','KEYCHAINS')
FASHION=category('CBT412060','Keychains','Clothing and Accessories','Fashion Accessories','KEYCHAINS')
MAT=category('CBT65841','Thermal Insulators','Sports and Fitness','Camping','CAMPING_THERMAL_MATS')
HEATER=category('CBT438142','Electric Heaters','Appliances','Space Heaters','ELECTRIC_HOME_HEATERS')
class FakeAPI:
    def category(self,cid):return copy.deepcopy({r['id']:r for r in [CAR,FASHION,MAT,HEATER]}[cid])
    def domain_discovery(self,site,query):return [{'category_id':CAR['id']}] if 'keychain' in query else [{'category_id':MAT['id']}]

class CategoryTests(unittest.TestCase):
    def test_new_category_attributes_require_selected_sku_evidence(self):
        from src.attribute_context_v3 import source_value_supported,valid_value
        from src.round2_evidence_v3 import candidate_attributes
        supplier={'selected_variant':'款式4-红色','attributes':{'图案':'十字架/皇冠/罗马数字'}}
        self.assertFalse(source_value_supported({'id':'COLOR','value_name':'Light coral'},supplier))
        self.assertFalse(source_value_supported({'id':'PATTERN_NAME','value_name':'Batman'},supplier))
        self.assertFalse(valid_value({'value_name':'No evidence provided'},{'id':'SEC_STAMP','value_type':'string'}))
        rows=candidate_attributes(supplier,[{'id':'COLOR','value_type':'string'},{'id':'PATTERN_NAME','value_type':'string'}])
        self.assertEqual({r['id']:r['value_name'] for r in rows},{'COLOR':'Red','PATTERN_NAME':'Cross/Crown/Roman Numerals'})
    def freeze_tree(self,root):
        import datetime
        p=root/'data/categories/taxonomy-CBT.json';p.parent.mkdir(parents=True)
        leaves=[]
        for c in [CAR,FASHION,MAT,HEATER]:leaves.append({**copy.deepcopy(c),'domain_id':c['settings']['catalog_domain']})
        p.write_text(json.dumps({'queried_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'leaves':leaves}),'utf-8')
    def test_same_name_domain_different_branch_recovers_fashion_from_full_tree(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.freeze_tree(root);d={'payload':{'category_id':CAR['id']},'evidence':{'source_title':'合金十字架饰品箱包挂件钥匙扣'}}
            result=resolve(root,d,FakeAPI());self.assertEqual(result['selected']['id'],FASHION['id'])
            self.assertTrue(any(x['category_id']==CAR['id'] and not x['accepted'] for x in result['candidates']))
            self.assertEqual(resolve(root,d,FakeAPI())['cached_decision'],True)
    def test_electric_foot_warmer_is_not_camping_mat_even_if_first(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.freeze_tree(root);d={'evidence':{'source_title':'办公桌下控温发热暖脚围垫'}}
            result=resolve(root,d,FakeAPI());self.assertEqual(result['selected']['id'],HEATER['id'])
            self.assertTrue(semantic_issues(d,MAT));self.assertFalse(semantic_issues(d,HEATER))
    def test_pet_filter_must_not_enter_dog_waste_other_branch(self):
        source={'source_title':'宠物饮水机滤芯猫狗喷泉过滤器'};intent=intent_for(source)
        bad=category('CBT434768','Other','Animals and Pets','Dogs','ANIMAL_AND_PET_PRODUCTS')
        bad['path_from_root'].insert(2,{'name':'Dog Waste Supplies'})
        self.assertFalse(assessment(bad,intent)[0])
    def test_bar_tip_is_a_garden_spare_part_not_a_whole_chainsaw(self):
        intent=intent_for({'source_title':'油锯大3/8导板头缓冲弹簧跨境'})
        whole=category('CBT373523','Combustion Chamber Chainsaws','Tools','Garden Tools','COMBUSTION_CHAINSAWS')
        self.assertFalse(assessment(whole,intent)[0])
        part=category('CBT413603','Other','Tools','Garden Tools','TOOL_ACCESSORIES_AND_REPLACEMENTS')
        part['path_from_root'].insert(2,{'name':'Replacement Parts'})
        self.assertTrue(assessment(part,intent)[0])
    def test_garden_carburetor_and_primer_component_are_separate_types(self):
        whole=intent_for({'source_title':'GX31 139F割草机化油器'})
        part=intent_for({'source_title':'割草机化油器油杯油泡总成通用款'})
        self.assertEqual(whole['domains'],['GARDEN_MACHINE_CARBURETORS'])
        self.assertEqual(part['domains'],['TOOL_ACCESSORIES_AND_REPLACEMENTS'])
        self.assertIsNone(intent_for({'source_title':'割草机油管油泡化油器维修件套'}))
    def test_changed_source_invalidates_cached_category(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.freeze_tree(root)
            first=resolve(root,{'evidence':{'source_title':'时尚钥匙扣'}},FakeAPI())
            second=resolve(root,{'evidence':{'source_title':'控温加热暖脚围'}},FakeAPI())
            self.assertNotEqual(first['source_signature'],second['source_signature']);self.assertEqual(second['selected']['id'],HEATER['id'])
    def test_rebind_all_skus_preserves_copy_cost_manual_locks_and_archives_attrs(self):
        new=copy.deepcopy(FASHION);new['all_attributes']=[{'id':'BRAND','value_type':'string','tags':{'required':True}},{'id':'MATERIAL','value_type':'string','tags':{}}]
        d={'payload':{'category_id':CAR['id']},'evidence':{'current_pricing_sku_id':'a','sku_details':[{'sku_id':s,'purchase_cost_cny':7.5,'available_quantity':200,'ai_content':{'category_id':CAR['id'],'description':'Human '+s,'attributes':[{'id':'CAR_BRAND','value_name':'wrong'},{'id':'BRAND','value_name':'禾环'}]}} for s in ['a','b']]}}
        apply_category(d,new,{'version':VERSION,'status':'SOURCE_AND_PATH_VERIFIED'})
        for s in d['evidence']['sku_details']:
            self.assertEqual(s['ai_content']['category_id'],FASHION['id']);self.assertEqual(s['purchase_cost_cny'],7.5);self.assertEqual(s['available_quantity'],200)
            self.assertEqual(s['ai_content']['description'],'Human '+s['sku_id']);self.assertFalse(any(a['id']=='CAR_BRAND' for a in s['ai_content']['attributes']))
            self.assertTrue(s['ai_content']['category_attribute_history'])
        original=copy.deepcopy(d);d['payload']['category_id']=CAR['id'];d['evidence']['sku_details'][1]['human_field_locks']={'category_id':True};d['evidence']['sku_details'][1]['ai_content']['category_id']=CAR['id'];frozen=copy.deepcopy(d)
        with self.assertRaisesRegex(ValueError,'人工锁'):apply_category(d,new,{})
        self.assertEqual(d,frozen)
