import json, tempfile, threading, unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from src import workbench_assistant as assistant
from src import workbench_v3
from tests.test_workbench_v3 import sample

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='assistant-fixture-',dir=Path(__file__).parent)
        self.root=Path(self.temp.name);(self.root/'drafts').mkdir()
    def tearDown(self): self.temp.cleanup()
    def draft(self,title='测试商品'):
        d=sample();d['payload']['title']=title
        path=self.root/'drafts/test-draft.json';path.write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8');return path
    def test_fresh_records_not_old_conversation(self):
        path=self.draft('旧标题');data=assistant.DataSource(self.root)
        self.assertEqual(data.inspect_product(path.name)['data']['product']['title'],'旧标题')
        self.draft('新标题')
        self.assertEqual(data.inspect_product(path.name)['data']['product']['title'],'新标题')
        self.assertEqual(data.get_workbench_overview()['data']['商品总数'],1)
        path.unlink()
        self.assertEqual(data.get_workbench_overview()['data']['商品总数'],0)
    def test_missing_product_is_explicit(self):
        self.assertFalse(assistant.DataSource(self.root).inspect_product('unknown-draft.json')['data']['found'])
    def test_path_traversal_and_absolute_paths_refused(self):
        for name in ('../outside-draft.json','..\\outside-draft.json','C:/outside-draft.json','.secrets','a/xx-draft.json'):
            with self.subTest(name=name),self.assertRaises(ValueError): assistant.DataSource(self.root).inspect_product(name)
    def test_cache_metadata_excludes_buyer_and_credentials(self):
        directory=self.root/'data/v3/operations/mercadolibre/test-store';directory.mkdir(parents=True)
        (directory/'orders.json').write_text(json.dumps({'state':'OK','time':'2026-01-01','total':1,'rows':[{'buyer':'PRIVATE_PERSON','email':'PRIVATE_EMAIL'}],'access_token':'PRIVATE_SECRET'}),encoding='utf-8')
        data=assistant.DataSource(self.root).get_operations_state();text=json.dumps(data)
        self.assertNotIn('PRIVATE',text);self.assertEqual(data['data']['records'][0]['platform_queried_at'],'2026-01-01')
        self.assertIn('非实时',data['source']['scope'])
    def test_raw_secrets_redacted_from_error_text(self):
        self.assertNotIn('abc123',assistant.clean('access_token=abc123 failed https://example.test/?secret=value'))
    def test_jobs_dont_accept_arbitrary_file_paths(self):
        with self.assertRaises(ValueError): assistant.job_status(self.root,'../drafts/test')
    def test_busy_does_not_start_another_model_job(self):
        assistant.RUN_LOCK.acquire()
        try:
            with patch.object(assistant,'runtime_paths',return_value=(self.root,Path(__file__))):
                with self.assertRaisesRegex(ValueError,'上一条'): assistant.ask(self.root,{'question':'现在卡哪里'})
        finally: assistant.RUN_LOCK.release()
    def test_http_routes_and_cross_origin_rejection(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),workbench_v3.make_handler(self.root));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/api/v3/assistant/status') as r: self.assertEqual(json.load(r)['mode'],'PREVIEW_THEN_CONFIRM_LOCAL')
            req=Request(base+'/api/v3/assistant/ask',data=b'{"question":"hello"}',headers={'Origin':'http://external.test','Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught: urlopen(req)
            self.assertEqual(caught.exception.code,400)
            with patch.object(assistant,'ask',return_value={'ok':True,'job_id':'test'}) as ask:
                workbench_v3.LOCK.acquire()
                try:
                    req=Request(base+'/api/v3/assistant/ask',data=b'{"question":"hello"}',headers={'Origin':base,'Content-Type':'application/json'})
                    with urlopen(req,timeout=2) as r: self.assertEqual(json.load(r)['job_id'],'test')
                    ask.assert_called_once()
                finally: workbench_v3.LOCK.release()
        finally: server.shutdown();server.server_close();thread.join()

class AnswerGuardTests(unittest.TestCase):
    def test_omitted_barcode_cannot_be_presented_as_complete_diagnosis(self):
        from src.workbench_assistant_worker import checked_product_answer
        checks=[{'label':'商品记录核对','title':'测试商品','status':'待审核','issues':['条码仍待确认','包装重量未确认'],'next_action':'人工审核'}]
        result=checked_product_answer('包装重量未确认，请调用get_product_logs',checks)
        self.assertIn('条码仍待确认',result)
        self.assertNotIn('get_product_logs',result)
        self.assertIn('当前状态：待审核',result)
    def test_summary_covering_evidence_is_preserved(self):
        from src.workbench_assistant_worker import checked_product_answer
        answer='条码与重量仍待确认，需人工审核。'
        self.assertEqual(checked_product_answer(answer,[{'label':'商品记录核对','issues':['条码待确认','重量待确认']}]),answer)

if __name__=='__main__':unittest.main()
