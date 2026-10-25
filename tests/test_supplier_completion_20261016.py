import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from src.supplier_facts_v3 import normalize_attributes,literal_attributes,wrong_type
from src.package_recovery_v3 import recover
from src.supplier_completion_v3 import apply

class SupplierCompletionTests(unittest.TestCase):
    def test_repeated_nested_brand_and_next_attribute_are_separated(self):
        result=normalize_attributes({'品牌':'品牌 璟途 璟途 璟途 货号 货号 QLMF-0103 QLMF-0103 QLMF-0103'})
        self.assertEqual(result['品牌'],'璟途');self.assertEqual(result['货号'],'QLMF-0103')
        rows=literal_attributes({'attributes':result},[{'id':'BRAND'},{'id':'MODEL'}])
        self.assertEqual(rows,[{'id':'BRAND','value_id':None,'value_name':'璟途'}])
    def test_source_conflict_does_not_choose_a_brand(self):
        result=normalize_attributes({'品牌':'品牌 A 品牌 B'})
        self.assertEqual(literal_attributes({'attributes':result},[{'id':'BRAND'}]),[])
    def test_unknown_brand_is_not_generic(self):
        self.assertEqual(literal_attributes({'attributes':{'品牌':'未标明'}},[{'id':'BRAND'}]),[])
    def test_door_banner_rejects_hardware_in_all_languages(self):
        source='亡灵节门帘门幅彩色骷髅装饰挂旗'
        for bad in ['Door Hinge','Bisagra Puerta','Dobradiça']:self.assertTrue(wrong_type(source,bad))
        self.assertFalse(wrong_type(source,'Door Hanging Banner'));self.assertFalse(wrong_type('实心门铰链','Door Hinge'))
    def fixture(self):
        return {'payload':{},'evidence':{'sku_details':[{'sku_id':'a','variant':'款式1 / 90x180cm','package':{'weight_g':120}}, {'sku_id':'b','variant':'款式12 / 90x180cm','package':{'weight_g':120}}],
            'supplier_page':{},'image_review':{'source_gallery':[{'remote_url':'https://cbu01.alicdn.com/a.jpg'}],'detail_gallery':[{'remote_url':'https://cbu01.alicdn.com/label.jpg'}]}}}
    def test_multisku_ocr_scans_common_label_once_but_requires_scope(self):
        d=self.fixture();inspect=Mock(return_value={'confidence':'high','length_cm':20,'width_cm':15,'height_cm':2,'evidence':['包装尺寸：20x15x2cm']})
        result=recover(Path('.'),d,d['evidence']['sku_details'],inspector=inspect)
        self.assertEqual(inspect.call_count,1);self.assertTrue(all(not r['recovered'] for r in result))
        self.assertEqual(len(d['evidence']['package_recovery']['unassigned_evidence']),1)
    def test_explicit_all_variant_package_can_fill_without_using_product_size(self):
        d=self.fixture();d['evidence']['supplier_page']['attributes']={'所有款式包装尺寸':'20x15x2cm','商品尺寸':'90x180cm'}
        result=recover(Path('.'),d,d['evidence']['sku_details'],inspector=Mock())
        self.assertEqual([r['recovered']['length_cm'] for r in result],[20,20])
    def test_sku1_quote_cannot_bind_sku12(self):
        d=self.fixture();d['evidence']['supplier_page']['attributes']={'款式1包装尺寸':'20x15x2cm'}
        result=recover(Path('.'),d,d['evidence']['sku_details'],inspector=Mock(return_value={}))
        self.assertEqual(result[0]['recovered']['height_cm'],2);self.assertEqual(result[1]['recovered'],{})
    def test_manual_package_is_not_overwritten(self):
        d=self.fixture();d['evidence']['sku_details'][0]['package']={'weight_g':120,'status':'USER_CONFIRMED'}
        d['evidence']['supplier_page']['attributes']={'所有款式包装尺寸':'20x15x2cm'}
        recover(Path('.'),d,d['evidence']['sku_details'],inspector=Mock())
        self.assertEqual(d['evidence']['sku_details'][0]['package']['length_cm'],20)
        self.assertEqual(d['evidence']['sku_details'][0]['package']['field_status']['weight_g'],'USER_CONFIRMED')
    def test_editor_binding_persists_to_exact_sku_and_listing(self):
        d=self.fixture();apply(d,{'sku_source_assignments':[{'sku_id':'b','reference':'https://cbu01.alicdn.com/a.jpg'}]})
        review=d['evidence']['image_review'];self.assertEqual(review['sku_source_galleries'][0]['sku_id'],'b')
        self.assertEqual(review['sku_listing_galleries'][0]['gallery'][0]['review_status'],'HUMAN_SELECTED')
        self.assertEqual(d['evidence']['sku_details'][0]['source_image_count'],0)
    def test_editor_cannot_bind_arbitrary_url_or_other_sku(self):
        for assignment in [{'sku_id':'z','reference':'https://cbu01.alicdn.com/a.jpg'},{'sku_id':'a','reference':'https://unknown.test/x.jpg'}]:
            with self.assertRaises(ValueError):apply(self.fixture(),{'sku_source_assignments':[assignment]})
    def test_bulk_dimensions_require_confirmation_and_do_not_promote_weight(self):
        d=self.fixture();confirm={'sku_ids':['a','b'],'length_cm':20,'width_cm':15,'height_cm':2,'evidence':'实测装袋包裹','confirmed':True}
        apply(d,{'sku_package_confirmations':[confirm]});self.assertEqual(d['evidence']['sku_details'][1]['package']['height_cm'],2)
        self.assertEqual(d['evidence']['sku_details'][1]['package']['field_status']['weight_g'],'UNVERIFIED')
        confirm['confirmed']=False
        with self.assertRaises(ValueError):apply(self.fixture(),{'sku_package_confirmations':[confirm]})
    def test_bulk_fill_preserves_existing_measurements(self):
        d=self.fixture();d['evidence']['sku_details'][0]['package']['length_cm']=21
        apply(d,{'sku_package_confirmations':[{'sku_ids':['a'],'length_cm':20,'width_cm':15,'height_cm':2,'evidence':'供应商','confirmed':True}]})
        self.assertEqual(d['evidence']['sku_details'][0]['package']['length_cm'],21)
    def test_category_guard_corrects_hinge_once_using_official_result(self):
        from src.category_guard_v3 import prepare
        d=self.fixture();d['payload']['category_id']='CBT432055';d['evidence']['source_title']='亡灵节门帘门幅骷髅挂旗'
        def query(cat,root):return {'category_id':cat,'name':'Hinges' if cat=='CBT432055' else 'Hangings','query_source':'MERCADOLIBRE_OFFICIAL_API_LIVE'}
        discover=Mock(return_value={'selected':query('CBT457468',None),'candidates':[{'domain_id':'CBT-DECORATIVE_PENNANTS_AND_GARLANDS','category_name':'Hangings','category_id':'CBT457468'}]})
        with tempfile.TemporaryDirectory() as folder:prepare(Path(folder),d,query,discover)
        self.assertEqual(d['payload']['category_id'],'CBT457468');self.assertEqual(discover.call_count,1)
    def test_transport_does_not_fall_back_on_http_authorization_error(self):
        import urllib.error
        from src.public_read_v3 import read
        with patch('urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://api.mercadolibre.com/x',403,'Forbidden',{},None)),patch('subprocess.run') as run:
            with self.assertRaises(urllib.error.HTTPError):read('https://api.mercadolibre.com/x')
            run.assert_not_called()
    def test_unbound_quality_screen_deduplicates_bytes_and_never_assigns_sku(self):
        from src.source_image_screening_v3 import precheck_unbound
        d=self.fixture();d['evidence']['image_review']['source_gallery'].append({'remote_url':'https://cbu01.alicdn.com/b.jpg'})
        verdict={'usable_candidate':True,'reason':'清晰原图','watermark':False,'promotional_poster':False,'blurred':False,'mixed_variants':False}
        response={'output':[{'content':[{'text':json.dumps(verdict)}]}]}
        with tempfile.TemporaryDirectory() as folder,patch('src.source_image_screening_v3._image_bytes',return_value=(b'photo-bytes','x.jpg','image/jpeg')),patch('src.local_model_runtime.responses',return_value=response) as model:
            precheck_unbound(Path(folder),d);precheck_unbound(Path(folder),d)
            self.assertEqual(model.call_count,1)
            self.assertEqual(d['evidence']['image_review']['source_quality_review']['usable_candidates'],1)
            self.assertNotIn('sku_source_galleries',d['evidence']['image_review'])
    def test_editor_save_retains_actual_binding_and_package_confirmation(self):
        from src.draft_editor import create_edited_version
        d=self.fixture();d['payload']={'title':'Banner','currency_id':'USD','sites_to_sell':[{'site_id':'MLM'}]}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'drafts').mkdir();(root/'drafts/test-draft.json').write_text(json.dumps(d),'utf-8')
            path=create_edited_version('test-draft.json',{'sku_source_assignments':[{'sku_id':'b','reference':'https://cbu01.alicdn.com/a.jpg'}],
                'sku_package_confirmations':[{'sku_ids':['b'],'length_cm':20,'width_cm':15,'height_cm':2,'evidence':'供应商确认','confirmed':True}]},root/'drafts',root/'draft_versions')
            saved=json.loads(path.read_text('utf-8'))
            self.assertEqual(saved['evidence']['sku_details'][1]['package']['height_cm'],2)
            self.assertEqual(saved['evidence']['image_review']['sku_listing_galleries'][0]['sku_id'],'b')
if __name__=='__main__':unittest.main()
