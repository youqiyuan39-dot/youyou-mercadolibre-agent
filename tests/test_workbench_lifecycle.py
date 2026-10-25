import importlib.util,json,socket,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import build_opener,ProxyHandler,Request
from urllib.error import HTTPError
from src.workbench_lifecycle import pause_jobs
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('backend_lifetime',ROOT/'desktop/backend_lifecycle.py');client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
class LifecycleTests(unittest.TestCase):
 def test_pause_only_unfinished_jobs_and_keep_outputs(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   root=Path(folder);(root/'jobs').mkdir()
   for n,status in [('active-full-ai-job','running'),('done-image-job','done'),('active-image-job','queued')]:
    (root/'jobs'/(n+'.json')).write_text(json.dumps({'status':status,'items':[{'status':'done','local_file':'keep.png'},{'status':'generating'}]}),encoding='utf-8')
   pause_jobs(root)
   done=json.loads((root/'jobs/done-image-job.json').read_text(encoding='utf-8'));active=json.loads((root/'jobs/active-image-job.json').read_text(encoding='utf-8'))
   self.assertEqual(done['status'],'done');self.assertEqual(active['status'],'paused');self.assertEqual(active['items'][0]['status'],'done');self.assertEqual(active['items'][1]['status'],'paused')
 def test_foreign_workspace_never_receives_stop(self):
  with patch.object(client,'build_opener') as op:
   life=client.BackendLifetime(ROOT,'http://127.0.0.1:1',lambda:{'workspace':str(ROOT.parent),'desktop_lifecycle':'WINDOW_BOUND_V1'})
   with self.assertRaises(RuntimeError):life.close()
   op.assert_not_called()
 def test_offline_owned_process_is_not_orphaned(self):
  proc=Mock();proc.poll.return_value=None
  with patch.object(client.time,'sleep'):
   life=client.BackendLifetime(ROOT,'http://127.0.0.1:1',lambda:None,proc);life.close();life.close()
  proc.terminate.assert_called_once();proc.wait.assert_called_once()
 def test_real_backend_and_collector_exit_on_desktop_close(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
   root=Path(folder);(root/'jobs').mkdir();(root/'jobs/demo-full-ai-job.json').write_text('{"status":"running"}',encoding='utf-8')
   code="""import sys,json
from pathlib import Path
from src.workbench_v3 import WorkbenchServer,make_handler
from src.workbench_lifecycle import bind
from src.collector_receiver import ensure_receiver
root=Path(sys.argv[1]);server=WorkbenchServer(('127.0.0.1',0),make_handler(root));bind(server,root)
collector,server.collector_status=ensure_receiver(root,port=0)
print(json.dumps({'port':server.server_port,'collector':collector.server_port}),flush=True)
try:server.serve_forever()
finally:
 server.server_close();collector.shutdown();collector.server_close()
"""
   proc=subprocess.Popen([sys.executable,'-X','utf8','-c',code,str(root)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
   try:
    ready=json.loads(proc.stdout.readline());base='http://127.0.0.1:'+str(ready['port']);op=build_opener(ProxyHandler({}))
    def health():
     try:return json.load(op.open(base+'/api/health',timeout=1))
     except OSError:return None
    self.assertEqual(health()['desktop_lifecycle'],'WINDOW_BOUND_V1')
    for body,origin in [(b'{}',None),(b'{"confirmed":true}','https://other.example')]:
     headers={'Content-Type':'application/json'}
     if origin:headers['Origin']=origin
     with self.assertRaises(HTTPError):op.open(Request(base+'/api/v3/desktop/shutdown',data=body,headers=headers))
     self.assertIsNotNone(health())
    client.BackendLifetime(root,base,health,proc).close();proc.wait(timeout=8);self.assertEqual(proc.returncode,0)
    for port in ready.values():
     with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',port)),0)
    self.assertEqual(json.loads((root/'jobs/demo-full-ai-job.json').read_text(encoding='utf-8'))['status'],'paused')
   finally:
    if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
    proc.stdout.close();proc.stderr.close()
if __name__=='__main__':unittest.main()
