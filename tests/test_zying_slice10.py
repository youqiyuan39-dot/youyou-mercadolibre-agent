"""Offline acceptance using copied real archive and explicitly reconstructed DOM."""
import copy,json,subprocess,tempfile,unittest,shutil
from pathlib import Path
from unittest.mock import Mock
from src import zying_detail_v3 as z
from src.category_resolution_v3 import resolve_zying_path
from src.draft_editor import browse_official_categories,EditorError
from src.collector_receiver import ensure_receiver
from urllib.request import Request,build_opener,ProxyHandler

class Slice10(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node=Path(__import__('tests.node_runtime',fromlist=['node']).node())
        cls.payload=json.loads(subprocess.check_output([str(node),str(Path(__file__).with_suffix('.cjs')),'--payload']).decode('utf-8'))
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config').mkdir();shutil.copy2(z.ROOT/'config/autopricing_policy.json',self.root/'config/autopricing_policy.json')
    def test_reconstructed_fields(self):
        d=z.normalize(self.payload,self.root)
        self.assertEqual([d['package'][k] for k in z.FIELDS],[10,10,10,146]);self.assertEqual(d['volumetric_weight_g'],166.67)
        self.assertEqual(d['brand'],'Generic');self.assertEqual(d['commission_pct'],18.5);self.assertEqual(d['reference']['shipping_cost_cny']['value'],'38.82')
        self.assertFalse(d['zying_profit_reference']['trial_calculated']);self.assertFalse(d['unshippable'])
    def test_price_reference_only(self):
        draft=z.build_draft(z.normalize(self.payload,self.root));p=draft['payload'];c=p['competitor_price']
        self.assertEqual(c['amount'],29.11);self.assertEqual(c['buyer_price_mxn'],549.63);self.assertEqual(c['source'],'competitor_price');self.assertTrue(c['reference_only'])
        self.assertNotIn('price',p);self.assertEqual(p['sites_to_sell'],[]);self.assertEqual(p['currency_id'],'USD')
    def test_missing_usd_never_convert(self):
        p=copy.deepcopy(self.payload);p['fields']['price']='MXN 549.63';p['field_origin'].pop('price_usd',None)
        d=z.normalize(p,self.root);self.assertIsNone(d['competitor_price']['amount']);self.assertIn('COMPETITOR_USD_PENDING',[r['code'] for r in d['input_requests']])
    def test_expired_cache_rejected(self):
        p=copy.deepcopy(self.payload);p['list_cache']['cached_at']='2020-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'过期'):z.normalize(p,self.root)
    def test_pending_requests(self):
        p=copy.deepcopy(self.payload);p['fields']['net_proceeds']=''
        d=z.normalize(p,self.root);codes=[r['code'] for r in d['input_requests']]
        self.assertIn('CATEGORY_MAPPING_PENDING',codes);self.assertIn('ZYING_NET_PROCEEDS_PENDING',codes)
        self.assertIn('Carburadores',d['category_path']);self.assertEqual(d['field_origin']['category_path'],'zying_list_cache')
    def test_official_denied_no_token_fallback(self):
        api=Mock();api.domain_discovery.side_effect=OSError('HTTP403')
        d=resolve_zying_path(self.root,self.payload['fields']['category_path'],api)
        self.assertEqual(d['status'],'CATEGORY_MAPPING_PENDING');api.domain_discovery.assert_called_once();api.category.assert_not_called()
    def test_official_exact_path_and_wrong_branch(self):
        path=self.payload['fields']['category_path'];names=['Herramientas','Herramientas de Jardín','Repuestos','Carburadores']
        api=Mock();api.domain_discovery.return_value=[{'category_id':'CBT123'}];api.category.return_value={'id':'CBT123','children_categories':[],'path_from_root':[{'name':n} for n in names]}
        self.assertEqual(resolve_zying_path(self.root,path,api)['status'],'SOURCE_AND_PATH_VERIFIED')
        api.category.return_value['path_from_root'][1]['name']='Vehicle Parts';self.assertEqual(resolve_zying_path(self.root,path,api)['status'],'CATEGORY_MAPPING_PENDING')
    def test_dialog_empty_root_is_pending(self):
        api=Mock();api.site_categories.return_value=[]
        d=browse_official_categories('CBT',client=api);self.assertFalse(d['is_leaf']);self.assertEqual(d['status'],'CATEGORY_MAPPING_PENDING');self.assertIn('未返回',d['message'])
        api.category.return_value={'id':'CBT1','path_from_root':[]};self.assertFalse(browse_official_categories('CBT','CBT1',client=api)['is_leaf'])
    def test_mapping_cache_mismatch_still_three_reads(self):
        from datetime import datetime,timezone
        p=self.root/'data/categories/taxonomy-CBT.json';p.parent.mkdir(parents=True)
        names=['Herramientas','Herramientas de Jardín','Repuestos','Carburadores']
        p.write_text(json.dumps({'queried_at':datetime.now(timezone.utc).isoformat(),'leaves':[{'category_id':'CBT123','path_from_root':[{'name':n} for n in names]}]}),encoding='utf-8')
        api=Mock();api.category.return_value={'id':'CBT999','path_from_root':[],'children_categories':[]}
        api.domain_discovery.return_value=[{'category_id':'CBT123'},{'category_id':'CBT456'}]
        result=resolve_zying_path(self.root,self.payload['fields']['category_path'],api)
        self.assertEqual(result['status'],'CATEGORY_MAPPING_PENDING')
        self.assertEqual(api.category.call_count+api.domain_discovery.call_count,3)
    def test_recapture_preserves_selected_category(self):
        detail=z.normalize(self.payload,self.root);old=z.build_draft(detail)
        old['payload']['category_id']='CBT123';old['edit_metadata']['field_locks']['category_id']=True
        fresh=z.merge_draft(old,detail)
        self.assertEqual(fresh['payload']['category_id'],'CBT123')
        self.assertNotIn('CATEGORY_MAPPING_PENDING',[r['code'] for r in fresh['input_requests']])
    def test_snapshot_archive_only(self):
        p={k:self.payload[k] for k in ('sku_id','site_id','source_url','captured_at')};p['snapshot']=self.payload['dom_snapshot']
        p['snapshot'].append({'label':'password','nodes':[{'tagName':'INPUT','value':'private-placeholder'}]})
        result=z.save_dom_snapshot(self.root,p);text=(self.root/result['payload_archive']).read_text('utf-8')
        self.assertTrue(Path(result['payload_archive']).name.startswith('dom_snapshot_'));self.assertNotIn('private-placeholder',text);self.assertFalse((self.root/'drafts').exists())
    def test_http_merge_idempotent_and_manual_preserved(self):
        server,_=ensure_receiver(self.root,0);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(operation,payload):
            req=Request(f'http://127.0.0.1:{server.server_port}/api/market-reference',json.dumps({'operation':operation,'detail':payload}).encode(),headers={'X-Youyou-Collector':'1','Content-Type':'application/json'})
            with build_opener(ProxyHandler({})).open(req,timeout=5) as r:return json.load(r)
        p=copy.deepcopy(self.payload);p['background_extension_version']='0.8.8'
        first=post('zying_detail',p);path=self.root/'drafts'/first['name'];old=json.loads(path.read_text('utf-8'));old['payload']['title']='OWNER EDIT';path.write_text(json.dumps(old),encoding='utf-8')
        second=post('zying_detail',p);self.assertEqual(first['name'],second['name']);self.assertEqual(len(list((self.root/'drafts').glob('*-draft.json'))),1)
        saved=json.loads(path.read_text('utf-8'));self.assertEqual(saved['payload']['title'],'OWNER EDIT');self.assertEqual(saved['evidence']['zying_detail']['brand'],'Generic');self.assertEqual(saved['payload']['competitor_price']['amount'],29.11)
        snap={k:p[k] for k in ('sku_id','site_id','source_url','captured_at')};snap['snapshot']=p['dom_snapshot'];self.assertFalse(post('zying_dom_snapshot',snap)['draft_modified'])
