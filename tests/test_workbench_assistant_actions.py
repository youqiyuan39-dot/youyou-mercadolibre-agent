import copy,json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from src import workbench_assistant as bridge,workbench_assistant_actions as actions,workbench_v3
from tests.test_workbench_v3 import sample

class ActionsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='actions-fixture-',dir=Path(__file__).parent);self.root=Path(self.temp.name)
  self.name='test-draft.json';self.draft=self.root/'drafts'/self.name;self.approval=self.root/'reviews/test-draft-approval.json'
  self.value=sample();self.value['payload'].update(price=123,available_quantity=5,description='原文');self.value['human_approved']=True
  bridge.write(self.draft,self.value);bridge.write(self.approval,{'approved':True});self.original=self.draft.read_bytes();self.original_review=self.approval.read_bytes()
 def tearDown(self):self.temp.cleanup()
 def prepare(self,kind='product',changes=None,name=None):
  return actions.prepare(self.root,kind,json.dumps(changes or {'title':'新标题'},ensure_ascii=False),'测试明确修改',self.name if name is None else name)['data']['id']
 def apply(self,ident,**kw):return actions.act(self.root,ident,'apply',{'confirmed':True,**kw})['change']
 def job(self,suffix='image-job',status='failed'):
  path=self.root/'jobs'/('test-draft-'+suffix+'.json');bridge.write(path,{'status':status,'created_at':'identity-1','items':[{'status':'failed'}]});return path
 def test_preview_never_changes_business_records(self):
  ident=self.prepare();self.assertEqual(self.original,self.draft.read_bytes());self.assertEqual(self.original_review,self.approval.read_bytes());self.assertEqual(actions.get(self.root,ident)['change']['state'],'pending')
 def test_confirm_required_and_cannot_repeat(self):
  ident=self.prepare()
  for confirm in (None,False,'true',1):
   with self.assertRaises(ValueError):actions.act(self.root,ident,'apply',{'confirmed':confirm})
  self.assertEqual(self.apply(ident)['state'],'applied')
  with self.assertRaisesRegex(ValueError,'重复'):self.apply(ident)
 def test_edit_preserves_unrelated_values_and_invalidates_approval(self):
  ident=self.prepare();result=self.apply(ident);after=bridge.read(self.draft)
  self.assertEqual(after['payload']['title'],'新标题');self.assertEqual(after['payload']['price'],123)
  self.assertEqual(after['payload']['attributes'],self.value['payload']['attributes']);self.assertEqual(after['evidence'],self.value['evidence'])
  self.assertFalse(after['human_approved']);self.assertFalse(bridge.read(self.approval)['approved']);self.assertTrue(result['result']['review_required'])
 def test_physical_values_never_become_confirmed_facts(self):
  ident=self.prepare(changes={'packed_weight_g':100,'packed_dimensions_cm':{'length':10,'width':5,'height':3}});self.apply(ident)
  evidence=bridge.read(self.draft)['evidence'];self.assertIn('UNVERIFIED',evidence['packed_weight']['status']);self.assertIn('UNVERIFIED',evidence['packed_dimensions']['status'])
 def test_forbidden_fields_and_path_traversal(self):
  for changes in ({'price':1},{'human_approved':True},{'api_key':'x'},{'available_quantity':True},{'packed_weight_g':float('nan')}):
   with self.subTest(changes=changes),self.assertRaises(ValueError):self.prepare(changes=changes)
  with self.assertRaises(ValueError):self.prepare(name='../outside-draft.json')
  with self.assertRaises(ValueError):actions.get(self.root,'../test')
  with self.assertRaises(ValueError):self.prepare('template',{'id':'../evil','name':'x','content':'text'},name='')
 def test_stale_draft_or_review_prevents_overwrite(self):
  ident=self.prepare();bridge.write(self.approval,{'approved':True,'new_review':True})
  with self.assertRaisesRegex(ValueError,'已有变动'):self.apply(ident)
  self.assertEqual(self.original,self.draft.read_bytes())
 def test_expired_or_cancelled_cannot_apply(self):
  ident=self.prepare();path=actions.change_path(self.root,ident);record=bridge.read(path);record['expires_epoch']=time.time()-1;bridge.write(path,record)
  with self.assertRaisesRegex(ValueError,'过期'):self.apply(ident)
  actions.act(self.root,ident,'cancel',{})
  with self.assertRaises(ValueError):self.apply(ident)
 def test_undo_restores_exact_original_bytes(self):
  ident=self.prepare();self.apply(ident);change=actions.act(self.root,ident,'undo',{'confirmed':True})['change']
  self.assertEqual(change['state'],'undone');self.assertEqual(self.original,self.draft.read_bytes());self.assertEqual(self.original_review,self.approval.read_bytes())
 def test_stale_undo_never_clobbers_new_edits(self):
  ident=self.prepare();self.apply(ident);bridge.write(self.draft,{'other_edit':True})
  with self.assertRaisesRegex(ValueError,'其他修改'):actions.act(self.root,ident,'undo',{'confirmed':True})
 def test_failed_second_write_restores_both_originals(self):
  ident=self.prepare();realwrite=bridge.write
  def injected(path,value):
   if path==self.approval and value.get('approved') is False:raise OSError('injected write failure')
   return realwrite(path,value)
  with patch.object(bridge,'write',side_effect=injected):result=self.apply(ident)
  self.assertEqual(result['state'],'failed_rolled_back');self.assertEqual(self.original,self.draft.read_bytes());self.assertEqual(self.original_review,self.approval.read_bytes())
 def test_active_tasks_block_product_and_global_parameter_changes(self):
  self.job(status='running')
  with self.assertRaisesRegex(ValueError,'仍在运行'):self.prepare()
  with self.assertRaisesRegex(ValueError,'仍在运行'):self.prepare('generation',{'image_count':3},name='')
 def test_generation_changes_only_public_parameters(self):
  ident=self.prepare('generation',{'image_count':3},name='');self.apply(ident)
  self.assertEqual(bridge.read(self.root/'data/v3/generation.json'),{'image_count':3,'main_background':'white','image_text':'none'})
  actions.act(self.root,ident,'undo',{'confirmed':True});self.assertFalse((self.root/'data/v3/generation.json').exists());self.assertEqual(self.original,self.draft.read_bytes())
 def test_prompt_binding_does_not_call_model(self):
  ident=self.prepare('product_prompt',{'content':'只能使用来源明确的商品事实。'})
  with patch('src.draft_editor.start_full_ai_flow') as start:self.apply(ident);start.assert_not_called()
  prompt=bridge.read(self.draft)['evidence']['prompt_template'];self.assertEqual(prompt['status'],'BOUND_FOR_NEXT_AI_REQUEST')
 def test_template_versions_do_not_rebind_product(self):
  before=self.draft.read_bytes();ident=self.prepare('template',{'id':'test','name':'测试','content':'内容一'},name='');self.apply(ident)
  second=self.prepare('template',{'id':'test','content':'内容二'},name='');self.apply(second)
  template=bridge.read(self.root/'data/v3/templates/test.json');self.assertEqual(template['version'],2);self.assertEqual(template['previous'][0]['content'],'内容一');self.assertEqual(before,self.draft.read_bytes())
 def test_paid_retry_needs_extra_confirmation_and_runs_at_most_once(self):
  self.job();ident=self.prepare('task',{'action':'retry_images'})
  with patch('src.draft_editor.start_image_generation',return_value={'status':'queued'}) as start:
   with self.assertRaisesRegex(ValueError,'API额度'):self.apply(ident)
   start.assert_not_called();self.assertEqual(self.apply(ident,confirm_paid=True)['state'],'applied');start.assert_called_once()
   with self.assertRaises(ValueError):self.apply(ident,confirm_paid=True)
 def test_unknown_retry_does_not_silently_start_new_job(self):
  with self.assertRaisesRegex(ValueError,'未找到'):self.prepare('task',{'action':'retry_full'})
 def test_failed_task_request_is_not_replayed(self):
  self.job('full-ai-job');ident=self.prepare('task',{'action':'retry_full'})
  with patch('src.draft_editor.start_full_ai_flow',side_effect=RuntimeError('injected engine failure')) as start:
   self.assertEqual(self.apply(ident,confirm_paid=True)['state'],'failed_or_uncertain')
   with self.assertRaises(ValueError):self.apply(ident,confirm_paid=True)
   start.assert_called_once()
 def test_pause_allows_progress_but_not_replaced_job(self):
  path=self.job(status='running');ident=self.prepare('task',{'action':'pause'});record=bridge.read(path);record['progress']=25;bridge.write(path,record)
  self.assertEqual(self.apply(ident)['state'],'applied');self.assertEqual(bridge.read(path)['status'],'paused')
  record.update(status='running',created_at='identity-2');bridge.write(path,record);ident=self.prepare('task',{'action':'pause'});record['created_at']='identity-3';bridge.write(path,record)
  with self.assertRaisesRegex(ValueError,'被替换'):self.apply(ident)
 def test_http_confirmation_and_same_origin(self):
  ident=self.prepare();server=ThreadingHTTPServer(('127.0.0.1',0),workbench_v3.make_handler(self.root));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
  try:
   with urlopen(base+'/api/v3/assistant/changes/'+ident) as r:self.assertEqual(json.load(r)['change']['state'],'pending')
   for origin,body in [('http://external.test',{'confirmed':True}),(base,{})]:
    req=Request(base+'/api/v3/assistant/changes/'+ident+'/apply',data=json.dumps(body).encode(),headers={'Origin':origin,'Content-Type':'application/json'})
    with self.assertRaises(HTTPError) as caught:urlopen(req)
    self.assertEqual(caught.exception.code,400)
   self.assertEqual(self.original,self.draft.read_bytes())
   req=Request(base+'/api/v3/assistant/changes/'+ident+'/apply',data=b'{"confirmed":true}',headers={'Origin':base,'Content-Type':'application/json'})
   with urlopen(req) as r:self.assertEqual(json.load(r)['change']['state'],'applied')
  finally:server.shutdown();server.server_close();thread.join()

class PauseLifetimeTests(unittest.TestCase):
 def test_inflight_image_finishes_once_and_pause_stops_next_call(self):
  from src import image_generation as images
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   root=Path(folder);path=root/'jobs/test-image-job.json';prompts=[{'prompt':'first'},{'prompt':'second'}]
   bridge.write(path,{'status':'queued','items':[{'status':'queued'},{'status':'queued'}]})
   def reply(*args):
    job=bridge.read(path);job['status']='paused';bridge.write(path,job);return b'\x89PNGfake-test-image',{}
   with patch.object(images,'load_config',return_value={}),patch.object(images,'call_image_api',side_effect=reply) as api,patch.object(images,'_persist_common_gallery') as persist:
    images.run_job(root,'test-draft.json',prompts,[],path)
    api.assert_called_once();persist.assert_called_once()
   result=bridge.read(path);self.assertEqual(result['status'],'paused');self.assertEqual(result['items'][0]['status'],'done');self.assertEqual(result['items'][1]['status'],'queued')
 def test_paused_disk_state_does_not_allow_overlapping_live_worker(self):
  from types import SimpleNamespace
  from src import image_generation as images
  entered=threading.Event();release=threading.Event()
  def full(path,root,job):entered.set();release.wait(3)
  legacy=SimpleNamespace(_run_full_ai_flow=full,_save_full_job=lambda *a,**k:None,EditorError=ValueError)
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder,patch.object(images,'run_job',lambda *a,**k:None):
   root=Path(folder);actions.install_task_tracking(legacy);thread=threading.Thread(target=legacy._run_full_ai_flow,args=(root/'test-draft.json',root,root/'job.json'));thread.start();self.assertTrue(entered.wait(1))
   try:
    with self.assertRaisesRegex(ValueError,'线程尚未结束'):actions.idle(root,'test-draft.json')
   finally:release.set();thread.join()
   actions.idle(root,'test-draft.json')
 def test_full_flow_keeps_pause_instead_of_overwriting_it(self):
  from types import SimpleNamespace
  from src import image_generation as images
  legacy=SimpleNamespace(_run_full_ai_flow=lambda *a:None,_save_full_job=lambda *a,**k:None,EditorError=ValueError)
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder,patch.object(images,'run_job',lambda *a,**k:None):
   path=Path(folder)/'job.json';bridge.write(path,{'status':'paused'});actions.install_task_tracking(legacy)
   with self.assertRaisesRegex(ValueError,'暂停请求'):legacy._save_full_job(path,{},stage='next')
   self.assertEqual(bridge.read(path)['status'],'paused')

if __name__=='__main__':unittest.main()
