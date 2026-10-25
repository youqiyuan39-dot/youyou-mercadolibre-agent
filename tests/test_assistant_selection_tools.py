import copy,json,tempfile,unittest
from pathlib import Path
from datetime import date,timedelta
from tests import test_selection_v3 as fixtures
from src import selection_v3 as s,workbench_assistant as assistant,selection_discovery_v3 as d

class AssistantSelectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
  self.root=Path(self.tmp.name);self.source=assistant.DataSource(self.root)
 def tearDown(self):self.tmp.cleanup()
 def snapshot(self):
  fixture=fixtures.SelectionEvidence();fixture.setUp();snap=copy.deepcopy(fixture.snapshot)
  snap['captured_at']=s.stamp();snap['page_state']={'loading':False,'row_count':1}
  snap['rows'][0]['cells'][8]=(date.today()-timedelta(days=5)).isoformat()
  snap['rows'][0]['cells'][4]='总量：\n25-50'
  return snap
 def test_empty_is_explicit_and_no_write(self):
  self.assertFalse(self.source.get_selection_batch()['data']['found'])
  self.assertFalse(self.source.get_selection_research()['data']['found'])
  self.assertEqual(self.source.get_selection_overview()['data']['batches'],[])
  self.assertEqual(list(self.root.iterdir()),[])
 def test_batch_recheck_preserves_monthly_unknown_and_disk(self):
  b=s.run(self.root,{'snapshot':self.snapshot(),'rules':{'leaf_category':'Ruedas'}})['batch']
  before={str(p):p.read_bytes() for p in self.root.rglob('*.json')}
  result=self.source.get_selection_batch(b['id'])['data']
  self.assertEqual(result['current_recheck_counts']['CONDITIONAL_LEAD'],1)
  self.assertIsNone(result['items'][0]['sales_30d'])
  self.assertEqual(result['current_recheck_counts']['CANDIDATE'],0)
  self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*.json')})
 def test_invalid_identifiers_refused(self):
  for value in ('../secret','C:/secret','x'):
   with self.assertRaises(ValueError):self.source.get_selection_batch(value)
   with self.assertRaises(ValueError):self.source.get_selection_research(value)
 def test_research_and_sourcing_boundaries(self):
  from src import selection_research_v3 as research
  b=s.run(self.root,{'snapshot':self.snapshot(),'rules':{'leaf_category':'Ruedas'}})['batch']
  c=research.create(self.root,{'batch_id':b['id'],'item_id':b['rows'][0]['item_id']})['case']
  result=self.source.get_selection_research(c['id'])['data']
  self.assertEqual(result['profit_state'],'REAL_PROFIT_UNVERIFIED')
  self.assertIn('1688',result['next_steps'][0])
  self.assertTrue(result['assessment']['missing'])
 def test_exploration_read_does_not_expire_or_start_task(self):
  t=d.create(self.root,{'mode':'focused','rules':{'leaf_category':'Ruedas'},'query':'ruedas','max_rounds':1},background=False)['task']
  before={str(p):p.read_bytes() for p in self.root.rglob('*.json')}
  self.assertEqual(self.source.get_selection_discovery()['data']['tasks'][0]['task_id'],t['id'])
  self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*.json')})
 def test_catalog_and_topic_routing(self):
  data=self.source.get_workbench_capabilities()['data']
  self.assertIn('calculate_profit',data['read_tools']);self.assertIn('get_business_knowledge',data['read_tools'])
  for name in ('get_selection_overview','get_selection_batch','get_selection_discovery','get_selection_research'):
   self.assertNotIn(name,data['read_tools']);self.assertNotIn(name,assistant.TOOLS)
  self.assertIn('AI选品模块已撤下',data['areas']['主线'])
  self.assertIn('不会启动选品任务',self.source.get_page_help('selection')['data']['help'])
  self.assertTrue(assistant.selection_question('overview','1688货源还缺什么'))
  self.assertTrue(assistant.selection_question('selection','现在怎么样'))
  self.assertFalse(assistant.selection_question('processing','这件商品生成失败'))
  self.assertFalse(assistant.selection_question('selection','你的功能范围有哪些，含选品吗'))
 def test_retired_selection_api_returns_410_without_tasks_or_writes(self):
  import threading,urllib.request,urllib.error
  from unittest.mock import patch
  from src import workbench_v3 as w,selection_research_v3 as research
  server=w.WorkbenchServer(('127.0.0.1',0),w.make_handler(self.root))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with patch.object(s,'run') as run,patch.object(d,'create') as create,patch.object(research,'investigate') as investigate:
    for method in ('GET','POST'):
     req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/v3/selection/run',data=b'{}' if method=='POST' else None,headers={'Content-Type':'application/json'},method=method)
     with self.subTest(method=method),self.assertRaises(urllib.error.HTTPError) as error:
      urllib.request.urlopen(req,timeout=5)
     self.assertEqual(error.exception.code,410)
     with error.exception as response:body=json.load(response)
     self.assertFalse(body['ok']);self.assertIn('AI选品模块已退出当前项目',body['error'])
    run.assert_not_called();create.assert_not_called();investigate.assert_not_called()
   self.assertEqual(list(self.root.iterdir()),[])
  finally:server.shutdown();server.server_close();thread.join()
 def test_answer_does_not_mix_exploration_tasks_or_promote_leads(self):
  from src.workbench_assistant_worker import checked_selection_answer
  data={'tasks':[{'status':'BUDGET_STOP','stop_reason':'轮数用完','rounds':[{'label':'A','feedback':{'new_leads':0,'seen_or_similar':5}}]},
      {'status':'BUDGET_STOP','rounds':[{'label':'OLD','feedback':{'new_leads':99}}]}], 'collector':{'enabled':False}}
  answer=checked_selection_answer('全部可发布',[('get_selection_discovery',data)])
  self.assertNotIn('99',answer);self.assertNotIn('全部可发布',answer)
  self.assertIn('新增线索0条',answer)
  batch={'stored_counts':{'CONDITIONAL_LEAD':32,'CANDIDATE':0},'current_recheck_counts':{'CONDITIONAL_LEAD':2,'NEEDS_EVIDENCE':30,'EXCLUDED':0},'items':[]}
  answer=checked_selection_answer('有32个已验证候选',[('get_selection_batch',batch)])
  self.assertIn('准确月销量粗筛候选0条',answer)
  self.assertIn('按今天日期复核原快照：有条件线索2条',answer)
  self.assertIn('未重新查询平台',answer)

if __name__=='__main__':unittest.main()
