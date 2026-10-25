import copy,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen,Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from src import workbench_v3 as w
from tests.test_workbench_v3 import sample

class DirectPublishTests(unittest.TestCase):
    def row(self,**values):
        return {'name':'test-draft.json','title':'test','stage':'READY_FOR_HUMAN_REVIEW',
                'publish_status':'pending','full_ai_status':'done','publish_ready':True,
                'approved':False,'errors':[],'publish_issues':[],'evidence_issues':[],**values}
    def test_completed_routes_without_approval(self):
        raw=self.row();before=copy.deepcopy(raw);result=w.publish_center_row(raw)
        self.assertEqual(raw,before)
        self.assertTrue(result['in_publish_center']);self.assertEqual(result['stage'],'READY_TO_PUBLISH')
        self.assertFalse(result['approved'])
    def test_evidence_blockers_remain_inside_center(self):
        result=w.publish_center_row(self.row(stage='BLOCKED_EVIDENCE',errors=['missing weight'],
                     publish_issues=['missing weight'],evidence_issues=['unknown category']))
        self.assertTrue(result['in_publish_center']);self.assertFalse(result['publish_ready'])
        self.assertEqual(result['stage'],'PUBLISH_BLOCKED')
        self.assertEqual(result['publish_issues'],['missing weight','unknown category'])
    def test_incomplete_jobs_never_migrate_despite_old_approval(self):
        for status in ['queued','running','paused','failed']:
            with self.subTest(status=status):
                result=w.publish_center_row(self.row(full_ai_status=status,approved=True,stage='READY_TO_PUBLISH'))
                self.assertFalse(result['in_publish_center'])
                self.assertNotEqual(result['stage'],'READY_TO_PUBLISH')
    def test_terminal_results_keep_actual_outcome(self):
        for status,stage in [('success','PUBLISHED'),('partial','PUBLISH_PARTIAL'),('pending_confirmation','PUBLISH_PENDING_CONFIRMATION')]:
            result=w.publish_center_row(self.row(publish_status=status,full_ai_status='failed'))
            self.assertTrue(result['in_publish_center']);self.assertEqual(result['stage'],stage)
    def test_legacy_review_visible_without_data_migration(self):
        result=w.publish_center_row(self.row(full_ai_status=None,publish_ready=False))
        self.assertTrue(result['in_publish_center']);self.assertEqual(result['stage'],'PUBLISH_BLOCKED')
    def test_unprocessed_draft_stays_processing(self):
        result=w.publish_center_row(self.row(stage='READY_FOR_AI',full_ai_status=None))
        self.assertFalse(result['in_publish_center']);self.assertEqual(result['stage'],'READY_FOR_AI')
    def test_http_queue_overview_and_review_redirect_no_file_writes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder);(root/'drafts').mkdir();(root/'jobs').mkdir()
            p=root/'drafts/test-draft.json';p.write_text(json.dumps(sample()),encoding='utf-8')
            (root/'jobs/test-draft-full-ai-job.json').write_text('{"status":"done"}',encoding='utf-8')
            before={str(p):p.read_bytes() for p in root.rglob('*.json')}
            server=ThreadingHTTPServer(('127.0.0.1',0),w.make_handler(root))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                for endpoint in ['/api/drafts','/api/v3/products']:
                    with urlopen(base+endpoint) as response:
                        row=json.load(response)['items'][0]
                        self.assertTrue(row['in_publish_center']);self.assertFalse(row['approved'])
                        self.assertEqual(row['stage'],'PUBLISH_BLOCKED')
                with patch.object(w.legacy, 'build_publish_preview', return_value={'valid':True}):
                    with urlopen(base+'/api/drafts/test-draft.json/publish-preview') as response:
                        preview=json.load(response)['preview']
                        self.assertFalse(preview['valid']);self.assertTrue(preview['preflight_issues'])
                with urlopen(base+'/api/v3/overview') as response:
                    overview=json.load(response);self.assertEqual(overview['pending_publish'],1);self.assertEqual(overview['processing'],0)
                for endpoint in ['/review','/legacy/review']:
                    with urlopen(base+endpoint) as response: self.assertTrue(response.geturl().endswith('/#publish'))
                for endpoint in ['/api/publish/batch','/api/drafts/test-draft.json/publish','/api/drafts/test-draft.json/publish-retry']:
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(base+endpoint,data=b'{"confirmed":true}',headers={'Content-Type':'application/json'}))
                    self.assertEqual(caught.exception.code,409)
                with urlopen(base+'/api/v3/export.xlsx?scope=pending') as response:
                    self.assertTrue(response.read().startswith(b'PK'))
                self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*.json')})
                self.assertFalse((root/'reviews').exists())
            finally:server.shutdown();server.server_close();thread.join()
