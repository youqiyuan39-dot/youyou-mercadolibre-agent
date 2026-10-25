import json, threading, unittest
from pathlib import Path
from urllib.request import urlopen
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from src import business_brief_v3 as brief, workbench_v3
from src.workbench_assistant import DataSource

def row(name, stage, **kw):
    return {'name':name+'-draft.json','title':name,'stage':stage,**kw}

class BusinessBriefTests(unittest.TestCase):
    def test_existing_success_not_misclassified_by_old_missing_facts(self):
        result=brief.build([row('old','PUBLISHED',evidence_issues=['包装重量未确认']),row('partial','PUBLISH_PARTIAL',publish_error='目标站点物流拒绝'),row('failed','AI_FAILED',full_ai_error='timed out'),row('new','PUBLISH_BLOCKED',in_publish_center=True,evidence_issues=['重量待核实'])])
        self.assertEqual([t['name'] for t in result['tasks']],['partial-draft.json','failed-draft.json','new-draft.json'])
        self.assertEqual(result['recorded_published'],1)
        self.assertIn('避免整件重复发布',result['tasks'][0]['next_step'])
    def test_runtime_error_and_all_fact_blockers_are_retained(self):
        result=brief.build([row('x','AI_FAILED',errors=[],full_ai_error='The read operation timed out',publish_issues=['目标站点未保存','条码待确认'],evidence_issues=['条码待确认'])])
        task=result['tasks'][0]
        self.assertEqual(task['issues'],['目标站点未保存','条码待确认','The read operation timed out'])
        self.assertIn('本次不启动生成',task['next_step'])
    def test_running_with_missing_facts_is_not_told_to_restart(self):
        result=brief.build([row('x','AI_PROCESSING',full_ai_status='running',evidence_issues=['重量待确认'])])
        self.assertEqual(result['tasks'][0]['category'],'等待当前任务')
        self.assertIn('不重复启动',result['tasks'][0]['next_step'])
    def test_publish_readiness_cannot_remove_system_gate_or_invent_roi(self):
        result=brief.build([row('ready','READY_TO_PUBLISH',in_publish_center=True)])
        self.assertIn('尚未验收',result['system_prerequisite'])
        self.assertEqual(result['tasks'][0]['category'],'核对并发布')
        self.assertIn('点击发布',result['tasks'][0]['next_step'])
        for key in ('manual_minutes_per_sku','model_cost_per_sku','rework_count','verified_profit'): self.assertIsNone(result['measurement'][key])
    def test_selected_product_never_leaks_other_products(self):
        rows=[row('a','AI_FAILED'),row('b','PUBLISH_BLOCKED')]
        with patch.object(DataSource,'rows',return_value=rows):
            source=DataSource('.')
            self.assertEqual(source.get_business_brief('b-draft.json')['data']['product_total'],1)
            self.assertEqual(source.get_business_brief('b-draft.json')['data']['tasks'][0]['name'],'b-draft.json')
            missing=source.get_business_brief('missing-draft.json')['data']
            self.assertFalse(missing['found']);self.assertEqual(missing['tasks'],[])
            with self.assertRaises(ValueError): source.get_business_brief('../b-draft.json')
    def test_truncation_keeps_total_and_real_scope(self):
        result=brief.build([row(str(n),'PUBLISH_BLOCKED') for n in range(8)])
        self.assertEqual(result['action_total'],8);self.assertEqual(len(result['tasks']),5);self.assertTrue(result['truncated'])
        self.assertIn('非实时平台状态',result['scope'])
    def test_http_and_overview_share_the_same_current_business_projection(self):
        rows=[row('old','PUBLISHED'),row('failed','AI_FAILED',full_ai_error='timeout')]
        server=ThreadingHTTPServer(('127.0.0.1',0),workbench_v3.make_handler(Path('.')))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(workbench_v3,'draft_rows',return_value=rows):
                with urlopen('http://127.0.0.1:'+str(server.server_port)+'/api/v3/business-brief') as response: data=json.load(response)
                self.assertEqual(data['action_total'],1);self.assertEqual(data['tasks'][0]['name'],'failed-draft.json')
                self.assertEqual(workbench_v3.business_brief.build(rows)['tasks'],data['tasks'])
        finally: server.shutdown();server.server_close();thread.join()

class BusinessAnswerTests(unittest.TestCase):
    def test_model_cannot_reassign_facts_or_fee_approval_to_assistant(self):
        from src.workbench_assistant_worker import checked_business_answer
        data=brief.build([row('failure','AI_FAILED',full_ai_error='timed out')])
        answer=checked_business_answer('由助手确认事实与重试费用，已修复。',data)
        self.assertIn('你确认事实与重试费用',answer)
        self.assertNotIn('由助手确认事实',answer)
        self.assertIn('timed out',answer);self.assertIn('未执行修改',answer)
    def test_existing_product_question_without_brief_remains_unchanged(self):
        from src.workbench_assistant_worker import checked_business_answer
        self.assertEqual(checked_business_answer('已准备预览',None),'已准备预览')

if __name__=='__main__': unittest.main()
