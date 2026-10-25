"""Local-only DOM offer intake HTTP replay, all business files in temporary roots."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError
from unittest.mock import patch
from src import offer_candidates_1688_v3 as mod
from src import zying_detail_v3 as z
from src.collector_receiver import ensure_receiver

FIX=Path(__file__).parent/'fixtures'

class DomIntake(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config').mkdir()
        for n in ('autopricing_policy.json','offer_match_policy.json'):
            shutil.copy2(z.ROOT/'config'/n,self.root/'config'/n)
        detail=json.loads((FIX/'zying_detail_test.json').read_text('utf-8'))
        self.name=z.save(self.root,detail)['name'];self.sid=detail['sku_id']
        self.value={'market_item_id':self.sid,'site_id':'MLM','source_url':'https://meli.zying.net/result?tracking=SECRET_FIXTURE',
            'captured_at':'2026-10-21T12:00:00Z','rate_limit':{'daily_count':1,'daily_limit':50,'interval_ms':5000,'day':'2026-10-21'},
            'page_count':1,'status':'READY','candidates':[{'offer_id':'900000001','offer_url':'https://detail.1688.com/offer/900000001.html?tracking=SECRET_FIXTURE',
                'title':'filter VF3500 black pack of 2','price':'18-20','currency':'CNY','spec_summary':'VF3500 black','min_order':'2件起批',
                'image_url':'https://cbu01.alicdn.com/test.jpg?tracking=SECRET_FIXTURE','shop_name':'Fixture factory'}]}
        server,_=ensure_receiver(self.root,0);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.url=f'http://127.0.0.1:{server.server_port}/api/market-reference'

    def post(self,value=None,raw=None,headers=None,operation='offer_candidates_1688'):
        data=raw if raw is not None else json.dumps({'operation':operation,'detail':value or self.value}).encode()
        req=Request(self.url,data,headers=headers or {'Content-Type':'application/json','X-Youyou-Collector':'1'})
        try:
            with build_opener(ProxyHandler({})).open(req,timeout=5) as r:return r.status,json.load(r)
        except HTTPError as e:
            with e:return e.code,json.load(e)

    def draft(self):return json.loads((self.root/'drafts'/self.name).read_text('utf-8'))

    def test_archive_redacts_and_truncates(self):
        self.value['cookie']='SECRET_FIXTURE';self.value['nested']={'token':'SECRET_FIXTURE'}
        self.value['candidates'][0]['shop_name']='X'*300
        code,r=self.post();self.assertEqual(code,200)
        archive=(self.root/r['payload_archive']).read_text('utf-8');self.assertNotIn('SECRET_FIXTURE',archive)
        self.assertEqual(len(json.loads(archive)['candidates'][0]['shop_name']),120)

    def test_binding_version_score_missing_facts_and_manual_preserved(self):
        self.value['candidates'][0]['title']=self.draft()['evidence']['zying_detail']['title']
        path=self.root/'drafts'/self.name;d=self.draft();d['payload']['title']='主人标题';d['manual']={'keep':True};path.write_text(json.dumps(d),encoding='utf-8')
        code,r=self.post();self.assertEqual(code,200);self.assertEqual(r['name'],self.name);self.assertEqual(r['status'],'SKIPPED')
        new=self.draft();self.assertEqual(new['payload'],d['payload']);self.assertEqual(new['manual'],d['manual']);self.assertEqual(new['evidence']['sku_details'],d['evidence']['sku_details'])
        report=new['evidence']['source_offer'];self.assertEqual(report['provider'],'zying_dom_candidates')
        c=report['rows'][0]['candidates'][0];self.assertGreater(c['confidence'],0)
        codes={i['code'] for i in c['issues']};self.assertIn('OFFER_SPEC_MISSING',codes);self.assertIn('OFFER_PACKAGE_MISSING',codes)
        self.assertTrue(new['input_requests']);self.assertFalse(new['human_approved']);self.assertEqual(len(report['dom_summary']['top3']),1)
        self.assertTrue(list((self.root/'draft_versions').glob('*before-save*')))
        self.assertFalse((self.root/'data/v3/auto-processing').exists())
        self.post();self.assertEqual(len(list((self.root/'drafts').glob('*-draft.json'))),1)

    def test_unbound_archive_without_creation(self):
        self.value['market_item_id']='MLM999999999';code,r=self.post();self.assertEqual(code,200);self.assertEqual(r['status'],'ARCHIVED_UNBOUND')
        self.assertTrue((self.root/r['payload_archive']).exists());self.assertEqual(len(list((self.root/'drafts').glob('*.json'))),1)

    def test_blocked_login_no_candidates_and_requests(self):
        self.value['status']='BLOCKED_ON_LOGIN';code,r=self.post();self.assertEqual(code,200);self.assertEqual(r['status'],'BLOCKED_ON_LOGIN')
        d=self.draft();self.assertEqual(d['evidence']['source_offer']['rows'][0]['candidates'],[])
        self.assertTrue(any('BLOCKED_ON_LOGIN' in str(x) for x in d['input_requests']))
        self.assertEqual(json.loads((self.root/r['payload_archive']).read_text('utf-8'))['candidates'],[])

    def test_empty_is_skipped(self):
        self.value['candidates']=[];self.assertEqual(self.post()[1]['status'],'SKIPPED')

    def test_rate_fields_rejected(self):
        for key,value in [('daily_count',51),('daily_count',True),('daily_count',0),('interval_ms',4999),('interval_ms','5000'),('daily_limit',100),('day','2026-10-20')]:
            with self.subTest(key=key,value=value):
                v=copy.deepcopy(self.value);v['rate_limit'][key]=value;self.assertEqual(self.post(v)[0],400)
        self.assertFalse((self.root/'data/v3/offer_candidates').exists())

    def test_mlm_page_bounds_links(self):
        for key,value in [('market_item_id','MLB1234567'),('site_id','MLB'),('page_count',2),('candidates',self.value['candidates']*41),('source_url','https://evil.invalid/')]:
            v=copy.deepcopy(self.value);v[key]=value;self.assertEqual(self.post(v)[0],400)
        v=copy.deepcopy(self.value);v['candidates'][0]['offer_id']='900000002';self.assertEqual(self.post(v)[0],400)

    def test_operation_body_limit_independent(self):
        body=json.dumps({'operation':'offer_candidates_1688','detail':self.value,'ignored':'x'*35000}).encode()
        self.assertEqual(self.post(raw=body)[0],200)
        # The limit must reject from headers before reading an oversized body.
        # Sending the entire body races socket close on Windows (RST), masking
        # the actual response. Leave the body unsent to verify early rejection.
        from http.client import HTTPConnection
        from urllib.parse import urlsplit
        url = urlsplit(self.url)
        connection = HTTPConnection(url.hostname, url.port, timeout=5)
        try:
            connection.putrequest('POST', url.path)
            connection.putheader('X-Youyou-Collector', '1')
            connection.putheader('Content-Length', '131073')
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())['step'], 'request_size')
        finally:
            connection.close()
        body=json.dumps({'operation':'zying_detail','detail':{},'ignored':'x'*35000}).encode()
        self.assertEqual(self.post(raw=body)[0],400)
        body=json.dumps({'operation':'resolve','ignored':'x'*13000}).encode()
        self.assertEqual(self.post(raw=body)[0],400)

    def test_origin_header_guard(self):
        self.assertEqual(self.post(headers={'Origin':'https://evil.invalid','X-Youyou-Collector':'1'})[0],403)
        self.assertEqual(self.post(headers={'Content-Type':'application/json'})[0],403)

    def test_preserve_existing_foreign_source_offer(self):
        path=self.root/'drafts'/self.name;d=self.draft();d['evidence']['source_offer']={'version':'HUMAN','state':'CONFIRMED'};path.write_text(json.dumps(d),encoding='utf-8')
        self.assertEqual(self.post()[1]['status'],'ARCHIVED_PRESERVED');self.assertEqual(self.draft(),d)

    def test_provider_does_not_invent_sku_price_pack_or_specs(self):
        row=mod.ZyingDomCandidates(mod.normalize(self.value)).search({})['candidates'][0]
        self.assertEqual(row['sku_identity'],{});sku=row['capture']['skus'][0]
        self.assertNotIn('price',sku);self.assertNotIn('package',sku);self.assertTrue(sku['sku_id'].startswith('DOM_OFFER_'))

    def test_newest_slice7_idempotent_draft_selected(self):
        path=self.root/'drafts'/self.name;d=self.draft();d['evidence']['zying_detail']['captured_at']='2000-01-01T00:00:00Z';path.write_text(json.dumps(d),encoding='utf-8')
        newer=copy.deepcopy(d);newer['evidence']['zying_detail']['captured_at']='2026-10-21T12:00:00Z';n='zying-newest-draft.json';(self.root/'drafts'/n).write_text(json.dumps(newer),encoding='utf-8')
        self.assertEqual(self.post()[1]['name'],n);self.assertEqual(self.draft(),d)

    def test_dom_fixture_to_real_http_and_list_summary(self):
        import subprocess
        node=Path(__import__('tests.node_runtime',fromlist=['node']).node())
        if not node.is_file():self.skipTest('fixture replay requires bundled Node')
        script="const fs=require('fs'),p=require('./collector_extension/offer_candidates_parser.js'),dom=require('./tests/zying_minidom.cjs');process.stdout.write(JSON.stringify(p.read(dom(fs.readFileSync('./tests/fixtures/offer_dom_normal.html','utf8')),'https://meli.zying.net/')));"
        proc=subprocess.run([str(node),'-e',script],cwd=z.ROOT,capture_output=True,check=True)
        self.value.update(json.loads(proc.stdout));code,r=self.post();self.assertEqual(code,200)
        self.assertEqual(r['status'],'SKIPPED');self.assertEqual(self.draft()['evidence']['source_offer']['dom_summary']['top3'][0]['price'],'¥18-20')
        from src.draft_editor import list_drafts
        row=next(x for x in list_drafts(self.root/'drafts',self.root) if x['name']==self.name)
        self.assertEqual(row['offer_candidates_1688']['status'],'SKIPPED')

    def test_low_score_conflict_remains_pending(self):
        self.value['candidates'][0]['title']='unrelated garden hose pack of 99'
        self.post();d=self.draft();c=d['evidence']['source_offer']['rows'][0]['candidates'][0]
        self.assertEqual(c['status'],'SKIPPED');self.assertTrue(any(x['code']=='OFFER_TITLE_LOW' for x in c['issues']))
        self.assertNotIn('CONFIRMED',json.dumps(d['evidence']['source_offer']))
