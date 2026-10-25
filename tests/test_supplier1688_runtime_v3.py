"""Local synthetic transport and vault tests; never contacts an external API."""
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError

from src import supplier1688_runtime as r
from src import supplier1688_setup as setup
from tests.test_supplier_api_response_v3 import response

DUMMY={'app_key':'00001234','app_secret':'SYNTHETIC_PRIVATE_SECRET','access_token':'SYNTHETIC_PRIVATE_TOKEN'}


class DetailRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        r.configure(self.root,{**DUMMY,'own_application_confirmed':True})
    def request(self,**kw):
        return {'product_id':'123456789','operation_id':uuid.uuid4().hex,'authorize_one_read':True,'no_extra_fee_confirmed':True,**kw}
    def transport(self):return patch.object(r,'_transport',return_value=json.dumps(response()).encode())

    def test_public_document_signature_vector(self):
        self.assertEqual(r._signature('param2/1/system/currentTime/1000000',{'a':'1','b':'2'},'test123'),
                         '33E54F4F7B989E3E0E912D3FBD2F1A03CA7CCE88')

    def test_status_does_not_decrypt_and_vault_is_not_plaintext(self):
        with patch.object(r.private_store,'load_private_json',side_effect=AssertionError('must not decrypt')):
            self.assertTrue(r.public_status(self.root)['configured'])
        stored=r.vault(self.root).read_bytes()
        self.assertTrue(all(value.encode() not in stored for value in DUMMY.values()))

    def test_no_permissions_or_fees_consent_never_sends_request(self):
        with self.transport() as transport:
            for kw in [{'authorize_one_read':False},{'no_extra_fee_confirmed':False},{'product_id':'bad'},{'operation_id':'bad'}]:
                with self.assertRaises(r.Error):r.fetch_detail(self.root,self.request(**kw))
            transport.assert_not_called()

    def test_exact_product_request_and_single_request_idempotency(self):
        value=self.request()
        with self.transport() as transport:
            first=r.fetch_detail(self.root,value);second=r.fetch_detail(self.root,value)
            self.assertEqual(transport.call_count,1)
            url,body=transport.call_args.args
            self.assertTrue(url.startswith('https://gw.open.1688.com/openapi/param2/1/com.alibaba.product/alibaba.product.get/'))
            from urllib.parse import parse_qs
            fields=parse_qs(body.decode('ascii'))
            self.assertEqual(fields['productID'],['123456789']);self.assertEqual(fields['webSite'],['1688'])
            self.assertTrue(first['audit']['business_identity_verified']);self.assertTrue(second['duplicate'])
            self.assertNotIn('app_key',first['audit'])

    def test_business_response_is_sanitized_before_any_storage_or_return(self):
        raw=response();raw['debug']={'access_token':DUMMY['access_token'],'message':'prefix '+DUMMY['app_secret']}
        with patch.object(r,'_transport',return_value=json.dumps(raw).encode()):result=r.fetch_detail(self.root,self.request())
        public=json.dumps(result)
        self.assertTrue(all(v not in public for v in DUMMY.values()))
        for p in r.location(self.root).rglob('*'):
            if p.is_file():self.assertTrue(all(v not in p.read_text('utf-8') for v in DUMMY.values()))
        self.assertEqual(result['response']['productInfo']['productID'],123456789)

    def test_network_failure_has_no_automatic_retry_or_context_echo(self):
        value=self.request()
        with patch.object(r,'_transport',side_effect=OSError('SYNTHETIC_PRIVATE_SECRET')) as transport:
            with self.assertRaises(r.Error) as caught:r.fetch_detail(self.root,value)
            self.assertNotIn(DUMMY['app_secret'],str(caught.exception))
            with self.assertRaises(r.Error):r.fetch_detail(self.root,value)
            self.assertEqual(transport.call_count,1)

    def test_http_failure_retains_only_fixed_diagnostic_category(self):
        value=self.request()
        raw=json.dumps({'error_code':'ISV.INVALID_SIGNATURE',
                        'error_message':'signature '+DUMMY['app_secret'],
                        'access_token':DUMMY['access_token']}).encode()
        with patch.object(r,'_transport',side_effect=r._HTTPFailure(400,raw)) as transport:
            with self.assertRaises(r.Error) as caught:r.fetch_detail(self.root,value)
            self.assertEqual(caught.exception.diagnostics,{'provider_category':'SIGNATURE_INVALID','provider_code':'ISV.INVALID_SIGNATURE','diagnostic_basis':'MESSAGE_KEYWORD_HEURISTIC','http_status':400})
            self.assertEqual(transport.call_count,1)
        for p in r.location(self.root).rglob('*.json'):
            text=p.read_text('utf-8')
            self.assertTrue(all(v not in text for v in DUMMY.values()))
            self.assertNotIn('error_message',text)
        self.assertNotIn(DUMMY['app_secret'],str(caught.exception))

    def test_arbitrary_error_body_is_never_echoed_as_provider_code(self):
        for raw in (b'<html>private error</html>',{'error_code':DUMMY['access_token']},[],None):
            output=r._provider_diagnostic(raw)
            self.assertNotIn(DUMMY['access_token'],json.dumps(output))
            self.assertIn('provider_category',output)

    def test_acl_error_code_takes_priority_over_appkey_word_in_message(self):
        d=r._provider_diagnostic({'error_code':'gw.APIACLDecline','error_message':'AppKey is declined'})
        self.assertEqual(d['provider_category'],'API_PERMISSION_REQUIRED')
        self.assertEqual(d['diagnostic_basis'],'PROVIDER_CODE')
        self.assertEqual(d['provider_code'],'gw.APIACLDecline')

    def test_provider_code_redacts_runtime_values_before_syntax_check(self):
        d=r._provider_diagnostic({'error_code':DUMMY['access_token']},list(DUMMY.values()))
        self.assertEqual(d['provider_code'],'SUPPRESSED')
        self.assertNotIn(DUMMY['access_token'],json.dumps(d))

    def test_unknown_envelope_is_preserved_but_cannot_import(self):
        value=self.request()
        with patch.object(r,'_transport',return_value=json.dumps({'result':response()}).encode()):out=r.fetch_detail(self.root,value)
        self.assertEqual(out['audit']['state'],'SCHEMA_REVIEW_REQUIRED')
        self.assertFalse(out['audit']['business_identity_verified'])
        with self.assertRaises(r.Error):r.preview_import(self.root,value['operation_id'])
        self.assertTrue((r.location(self.root)/'responses'/(value['operation_id']+'.json')).is_file())

    def test_wrong_product_not_marked_verified_and_error_body_not_saved(self):
        raw=response();raw['productInfo']['productID']=987654321
        with patch.object(r,'_transport',return_value=json.dumps(raw).encode()):out=r.fetch_detail(self.root,self.request())
        self.assertFalse(out['audit']['business_identity_verified'])
        raw={'error_code':'INVALID','error_message':DUMMY['access_token']};value=self.request()
        with patch.object(r,'_transport',return_value=json.dumps(raw).encode()):
            with self.assertRaises(r.Error):r.fetch_detail(self.root,value)
        self.assertFalse((r.location(self.root)/'responses'/(value['operation_id']+'.json')).exists())

    def test_redirects_refused_and_no_user_supplied_gateway(self):
        with self.assertRaises(r.Error):r.NoRedirect().redirect_request(None,None,302,'','', 'https://example.com')
        with self.transport() as transport:r.fetch_detail(self.root,self.request(endpoint='https://example.com'))
        self.assertIn('gw.open.1688.com',transport.call_args.args[0])

    def test_live_provenance_import_uses_existing_intake_and_queue(self):
        value=self.request()
        with self.transport():r.fetch_detail(self.root,value)
        imported=r.import_result(self.root,{'result_id':value['operation_id'],'confirmed':True})
        self.assertEqual(imported['draft'],'1688-123456789-draft.json')
        self.assertEqual(imported['auto_processing']['status'],'pending')
        again=r.import_result(self.root,{'result_id':value['operation_id'],'confirmed':True})
        self.assertTrue(again['auto_processing']['duplicate'])
        self.assertFalse(imported['publishing_performed'])


class DetailRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.patch=patch.object(setup,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.server=setup.ThreadingHTTPServer(('127.0.0.1',0),setup.Handler);self.server.supplier_setup_only=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close);self.base='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
    def post(self,path,data,origin=None):
        req=Request(self.base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Origin':origin or self.base})
        return build_opener(ProxyHandler({})).open(req,timeout=2)
    def test_config_secret_not_echoed_and_status_safe(self):
        with self.post('/api/v3/supplier1688/configure',{**DUMMY,'own_application_confirmed':True}) as response:body=response.read().decode()
        self.assertTrue(all(v not in body for v in DUMMY.values()))
        with build_opener(ProxyHandler({})).open(self.base+'/api/v3/supplier1688/status') as response:status=json.load(response)
        self.assertTrue(status['configured']);self.assertTrue(status['setup_only'])
    def test_foreign_origin_and_setup_import_rejected(self):
        with self.assertRaises(HTTPError) as caught:self.post('/api/v3/supplier1688/configure',DUMMY,'https://external.invalid')
        self.assertEqual(caught.exception.code,400)
        with self.assertRaises(HTTPError) as caught:self.post('/api/v3/supplier1688/import',{'confirmed':True})
        self.assertIn('WORKBENCH_RESTART_REQUIRED',caught.exception.read().decode())


if __name__=='__main__':unittest.main()
