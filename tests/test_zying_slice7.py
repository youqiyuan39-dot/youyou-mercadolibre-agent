"""Offline HTTP replays; reconstructed evidence is not an original browser payload."""
import copy
import json
import shutil
import subprocess
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from unittest.mock import patch
from src import zying_detail_v3 as z
from src import pricing_result_v3 as pricing
from src.collector_receiver import ensure_receiver

FIX=Path(__file__).parent/'fixtures'


class Slice7(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config').mkdir()
        shutil.copy2(z.ROOT/'config/autopricing_policy.json',self.root/'config/autopricing_policy.json')
        self.payload=json.loads((FIX/'zying_detail_test.json').read_text(encoding='utf-8'))
        self.payload.update(extension_version='0.8.5',parser_version='ZYING_DOM_20261021_SLICE7',
            diagnostics={'fields':{'weight':{'hit':False,'reason':'input 值为空'}},
                         'snippets':[{'kind':'label_value','text':'商品重量(g) = '}],'attempts':10})

    def start(self):
        server,_=ensure_receiver(self.root,0)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}/api/market-reference'
        def post(value):
            req=Request(url,json.dumps({'operation':'zying_detail','detail':value}).encode(),
                headers={'X-Youyou-Collector':'1','Content-Type':'application/json'})
            with build_opener(ProxyHandler({})).open(req,timeout=5) as response:return json.load(response)
        return post

    def load(self,name):return json.loads((self.root/'drafts'/name).read_text(encoding='utf-8'))

    def test_reconstructed_real_shape_http_archive_idempotency(self):
        post=self.start();rows=json.loads((FIX/'zying_slice7_reconstructed.json').read_text(encoding='utf-8'))
        names=[]
        for value in rows:
            self.assertIn('RECONSTRUCTED',value['reconstruction_note'])
            response=post(value);names.append(response['name'])
            archive=json.loads((self.root/response['payload_archive']).read_text(encoding='utf-8'))
            self.assertIn('RECONSTRUCTED',archive['reconstruction_note'])
            self.assertEqual(self.load(response['name'])['evidence']['project_contribution']['decision'],'PENDING')
        self.assertEqual(names[0],names[1]);self.assertNotEqual(names[0],names[2])
        self.assertEqual(len(list((self.root/'drafts').glob('*.json'))),2)
        self.assertEqual(len(list((self.root/'data/v3/zying_detail_payloads').glob('*/*.json'))),3)

    def test_archive_diagnostics_versions_and_recursive_redaction(self):
        post=self.start();value=copy.deepcopy(self.payload)
        value['nested']={'cookie':'FAKE_TEST','authorization':'FAKE_TEST','access_token':'FAKE_TEST'}
        value['main_image']='https://example.invalid/product.jpg?token=FAKE_TEST'
        value['diagnostics']['snippets'][0]['text']='a'*400
        response=post(value);d=self.load(response['name'])['evidence']['zying_detail']
        archive=(self.root/response['payload_archive']).read_text(encoding='utf-8')
        self.assertNotIn('FAKE_TEST',archive)
        self.assertEqual(d['extension_version'],'0.8.5');self.assertEqual(d['parser_version'],value['parser_version'])
        self.assertEqual(d['diagnostics']['fields']['weight']['reason'],'input 值为空')
        self.assertEqual(len(d['diagnostics']['snippets'][0]['text']),200)
        self.assertTrue((self.root/d['payload_archive']).is_file())

    def test_merge_preserves_manual_fields_locks_and_nonempty_capture(self):
        post=self.start();first=post(self.payload);path=self.root/'drafts'/first['name']
        draft=self.load(first['name']);draft['payload']['title']='主人标题';draft['payload']['description']='人工文案'
        draft['evidence']['sku_details'][0]['package']['weight_g']=777
        draft['evidence']['sku_details'][0]['purchase_cost_cny']=12
        draft['edit_metadata']['field_locks']['title']=True
        draft['edit_metadata']['field_locks']['weight_g']=True
        draft['custom_manual']={'keep':True};path.write_text(json.dumps(draft),encoding='utf-8')
        value=copy.deepcopy(self.payload);value['fields']['weight']='';value['fields']['dimensions']='-';value['main_image']=''
        value['fields']['seller']='NEW SELLER'
        second=post(value);result=self.load(second['name'])
        self.assertEqual(first['name'],second['name'])
        self.assertEqual(result['payload'],draft['payload'])
        self.assertEqual(result['custom_manual'],draft['custom_manual'])
        self.assertEqual(result['evidence']['sku_details'][0]['package']['weight_g'],777)
        self.assertEqual(result['evidence']['zying_detail']['package']['weight_g'],2500)
        self.assertEqual(result['evidence']['zying_detail']['package']['length_cm'],32)
        self.assertEqual(second['status'],'CAPTURE_READY')
        self.assertEqual(result['evidence']['zying_detail']['seller'],'NEW SELLER')
        self.assertTrue(result['evidence']['image_review']['source_gallery'])
        backup=list((self.root/'draft_versions').glob('*before-save*'))
        self.assertEqual(json.loads(backup[0].read_text(encoding='utf-8')),draft)
        self.assertFalse((self.root/'data/v3/auto-processing').exists())
        value['fields']['weight']='999 g';post(value)
        locked=self.load(second['name'])['evidence']
        self.assertEqual(locked['packed_weight']['value'],2500)
        self.assertEqual(locked['sku_details'][0]['package']['weight_g'],777)
        self.assertEqual(locked['zying_detail']['package']['weight_g'],999)

    def test_concurrent_same_item_http_one_draft(self):
        post=self.start()
        with ThreadPoolExecutor(max_workers=4) as pool: rows=list(pool.map(post,[self.payload]*4))
        self.assertEqual(len(set(r['name'] for r in rows)),1)
        self.assertEqual(len(list((self.root/'drafts').glob('*-draft.json'))),1)
        self.assertEqual(len(list((self.root/'data/v3/zying_detail_payloads').glob('*/*.json'))),4)

    def test_versioned_recapture_does_not_wake_existing_queue(self):
        from src import draft_editor as editor,auto_processing_v3 as auto
        post=self.start();first=post(self.payload)
        entry=self.root/'data/v3/auto-processing'/(Path(first['name']).stem+'.json')
        entry.parent.mkdir(parents=True);entry.write_text('{"status":"pending"}',encoding='utf-8')
        before=entry.read_bytes();auto.install_edit_resume(editor)
        self.payload['fields']['weight']='999 g'
        with patch.object(auto,'enqueue',side_effect=AssertionError('capture must not enqueue')):
            post(self.payload)
        self.assertEqual(entry.read_bytes(),before)

    def budget_draft(self):
        draft=z.build_draft(z.normalize(self.payload,self.root))
        sku=draft['evidence']['sku_details'][0]
        sku.update(purchase_cost_cny=10,domestic_shipping_cny=0,shipping_cost_cny=10,
                   cny_per_mxn='0.36',fx_source='OFFLINE_TEST quote 2026-10-21 CNY per MXN')
        return draft

    def test_profit_three_states_and_exact_ten(self):
        draft=self.budget_draft();detail=draft['evidence']['zying_detail'];detail['buyer_price']['MXN']=100
        p=json.loads((self.root/'config/autopricing_policy.json').read_text(encoding='utf-8'))
        sku=draft['evidence']['sku_details'][0]
        sku.update(shipping_cost_cny=0,purchase_cost_cny=0,cny_per_mxn=1)
        detail['commission_pct']=0
        # Existing reserves: 90 - base*1.03; base=80/1.03 => exact 10% (Decimal).
        p.update(ads_net_pct='0',tax_reserve_net_pct='0',returns_base_pct='0',packing_service_cny='0',packaging_material_cny='0',other_cny='0')
        (self.root/'config/autopricing_policy.json').write_text(json.dumps(p),encoding='utf-8')
        for purchase,decision in ((89,'PASS'),(90,'BELOW_FLOOR'),(91,'BELOW_FLOOR')):
            sku['purchase_cost_cny']=purchase
            result=pricing.contribution(self.root,draft)
            self.assertEqual(result['decision'],decision)
            self.assertEqual(Decimal(result['unit_contribution']),100-purchase)
        del sku['cny_per_mxn'];self.assertEqual(pricing.contribution(self.root,draft)['decision'],'PENDING')

    def test_policy_reserves_commission_priority_no_reference_profit(self):
        draft=self.budget_draft();result=pricing.contribution(self.root,draft)
        self.assertEqual(result['inputs']['commission_pct'],'15.5')
        self.assertEqual(result['inputs']['policy']['returns_base_pct'],'3')
        c=result['inputs'];profit=Decimal(c['net_proceeds'])*Decimal('.90')-Decimal('20.6')*Decimal('1.03')/Decimal('.36')
        self.assertEqual(Decimal(result['unit_contribution']),profit)
        draft['evidence']['zying_profit_reference']={'margin_hint':'BELOW_FLOOR','net_profit':[{'value':-9999}]}
        self.assertEqual(pricing.contribution(self.root,draft),result)
        draft['evidence']['zying_detail']['commission_pct']=None
        self.assertEqual(pricing.contribution(self.root,draft)['inputs']['commission_pct'],'18')

    def test_all_missing_visible_and_fillable_without_remote_fx(self):
        post=self.start();response=post(self.payload)
        from src.input_requests_v3 import aggregate,apply_updates
        rows=aggregate(self.root)['rows']
        fields=('purchase_cost_cny','domestic_shipping_cny','shipping_cost_cny','cny_per_mxn','fx_source')
        for field in fields:self.assertTrue(any(r['field']==field and r['editable'] for r in rows),field)
        draft=self.load(response['name'])
        updates=[{'sku_id':self.payload['sku_id'],'field':f,'value':v,'source':'CONTRIBUTION_INPUT'}
                 for f,v in zip(fields,(10,0,10,.36,'OFFLINE quote 2026-10-21'))]
        from src.draft_editor import save_edited_draft
        save_edited_draft(response['name'],{},self.root/'drafts',self.root/'draft_versions',field_updates=updates)
        after=self.load(response['name'])
        self.assertNotEqual(after['evidence']['project_contribution']['decision'],'PENDING')
        self.assertFalse(any(r.get('origin')=='CONTRIBUTION_INPUT' for r in after['input_requests']))

    def test_list_duplicates_only_ui_and_old_prefill_recapture(self):
        post=self.start();saved=post(self.payload);first=self.root/'drafts'/saved['name'];data=first.read_bytes()
        other=first.with_name('zying-copy-draft.json');other.write_bytes(data)
        from src.draft_editor import list_drafts
        rows=list_drafts(self.root/'drafts',self.root)
        self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['duplicate_drafts']),1)
        self.assertEqual(first.read_bytes(),data);self.assertEqual(other.read_bytes(),data)
        draft=self.load(saved['name']);detail=draft['evidence']['zying_detail']
        del detail['field_origin'];del detail['zying_profit_reference']
        first.write_text(json.dumps(draft),encoding='utf-8');other.unlink() # disposable fixture only
        self.assertEqual(post(self.payload)['name'],saved['name'])

    def test_invalid_and_oversize_do_not_write(self):
        for value in ({**self.payload,'sku_id':'MLB123456789'}, {**self.payload,'huge':'x'*33000}):
            with self.assertRaises(ValueError):z.save(self.root,value)
        self.assertFalse((self.root/'drafts').exists())

    def test_dom_parser_diagnostics_real_http_pipeline(self):
        node=shutil.which('node') or str(Path(__import__('tests.node_runtime',fromlist=['node']).node()))
        output=self.root/'dom_payload.json';env=os.environ.copy();env['ZYING_SLICE7_DOM_OUT']=str(output)
        result=subprocess.run([node,str(Path(__file__).with_suffix('.cjs'))],env=env,
                              capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(output.read_text(encoding='utf-8'))
        response=self.start()(payload);detail=self.load(response['name'])['evidence']['zying_detail']
        self.assertEqual(detail['package']['weight_g'],550)
        self.assertEqual(detail['diagnostics']['attempts'],3)
        self.assertTrue(detail['diagnostics']['fields']['dimensions']['hit'])
        self.assertEqual(detail['extension_version'],'0.8.5')
        self.assertEqual(detail['parser_version'],'ZYING_DOM_20261022_SLICE9')

    def test_contribution_three_states_http_recapture_and_fill(self):
        p=json.loads((self.root/'config/autopricing_policy.json').read_text(encoding='utf-8'))
        p.update(ads_net_pct='0',tax_reserve_net_pct='0',returns_base_pct='0',packing_service_cny='0',packaging_material_cny='0',other_cny='0')
        (self.root/'config/autopricing_policy.json').write_text(json.dumps(p),encoding='utf-8')
        self.payload['fields'].update(price='MXN 100',commission='0%')
        post=self.start();response=post(self.payload);sid=self.payload['sku_id']
        from src.draft_editor import save_edited_draft
        from src.input_requests_v3 import aggregate
        for cost,expected in ((89,'PASS'),(90,'BELOW_FLOOR')):
            updates=[{'sku_id':sid,'field':f,'value':v,'source':'CONTRIBUTION_INPUT'} for f,v in
                     [('purchase_cost_cny',cost),('domestic_shipping_cny',0),('shipping_cost_cny',0),('cny_per_mxn',1),('fx_source','OFFLINE quote 2026-10-21')]]
            save_edited_draft(response['name'],{},self.root/'drafts',self.root/'draft_versions',field_updates=updates)
            repeat=post(self.payload);self.assertEqual(repeat['name'],response['name'])
            self.assertEqual(self.load(response['name'])['evidence']['project_contribution']['decision'],expected)
        draft=self.load(response['name']);del draft['evidence']['sku_details'][0]['cny_per_mxn']
        (self.root/'drafts'/response['name']).write_text(json.dumps(draft),encoding='utf-8')
        post(self.payload)
        self.assertEqual(self.load(response['name'])['evidence']['project_contribution']['decision'],'PENDING')
        self.assertTrue(any(r['field']=='cny_per_mxn' for r in aggregate(self.root)['rows']))


if __name__=='__main__':unittest.main()
