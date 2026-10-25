"""Offline engine/guard tests. Scripted selectors are NOT real model validation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from src.product_audit_agent import Audit, run, persist, job_path, save_candidate, start, verify_stopped_result

def draft():
    return {'payload':{'title':'Sprayer 500ml','description':'500ml','attributes':[]},'evidence':{'supplier_url':'https://detail.1688.com/offer/123.html','supplier_page':{'title':'Sprayer 250ml / 500ml'},'sku_details':[{'sku_id':'small','variant':'250ml'},{'sku_id':'large','variant':'500ml'}],'current_pricing_sku_id':'small','image_review':{'source_gallery':[],'sku_source_galleries':[]},'packed_weight':{'value':100,'status':'AI_ESTIMATE'}},'edit_metadata':{'field_locks':{}}}
def selector(steps,feedback=None):
    actions=iter(steps)
    def choose(request):
        if feedback:feedback(request)
        name,extra=next(actions)
        return {'status':'completed','usage':{'input_tokens':10,'output_tokens':5},'output':[{'type':'function_call','call_id':'call_'+str(len(request['input'])),'name':name,'arguments':json.dumps({'reason':'根据当前取得的证据选择下一步',**extra})}]}
    return choose

class ProductAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.d=draft()
    def tearDown(self):self.temp.cleanup()
    def test_feedback_repair_and_human_stop_preserves_source(self):
        before=copy.deepcopy(self.d);requests=[]
        actions=[('inspect_sku',{}),('propose_copy',{'title':'Sprayer 250ml','description':'250ml','evidence_ids':['sku']}),('verify_candidate',{}),('request_human',{'question':'请提供所选250ml商品的实测包装重量和尺寸。'})]
        result=run(self.root,'x-draft.json',self.d,selector(actions,lambda r:requests.append(copy.deepcopy(r))))
        self.assertEqual(result['state'],'NEEDS_HUMAN');self.assertEqual(self.d,before)
        self.assertIn('包装重量',json.dumps(requests[-1],ensure_ascii=False))
        self.assertEqual(result['candidate']['payload']['title'],'Sprayer 250ml')
        self.assertFalse(result['publication_allowed']);self.assertTrue(result['candidate_verified'])
    def test_different_path_can_stop_without_copy_generation(self):
        result=run(self.root,'x-draft.json',self.d,selector([('inspect_packaging',{}),('request_human',{'question':'请提供实测包装依据，不使用估算值。'})]))
        self.assertEqual([t['tool'] for t in result['trace']],['inspect_packaging','request_human'])
        self.assertFalse(result['candidate_changed'])
    def test_unknown_tool_and_premature_finish_are_feedback_errors(self):
        result=run(self.root,'x-draft.json',self.d,selector([('publish',{}),('finish',{}),('request_human',{'question':'请确认缺失资料后再继续核查。'})]))
        self.assertIn('error',result['trace'][0]['observation']);self.assertIn('error',result['trace'][1]['observation'])
        self.assertEqual(result['candidate'],self.d)
    def test_manual_lock_and_invalid_selected_sku(self):
        self.d['edit_metadata']['field_locks']['title']=True
        a=Audit(self.root,'x-draft.json',self.d);a.execute('inspect_sku',{'reason':'读取SKU'})
        with self.assertRaisesRegex(ValueError,'人工锁定'):a.execute('propose_copy',{'reason':'修订','title':'Sprayer 250ml','description':'250ml','evidence_ids':['sku']})
        self.d['evidence']['current_pricing_sku_id']='missing';a=Audit(self.root,'x-draft.json',self.d);a.execute('inspect_sku',{'reason':'读取SKU'})
        with self.assertRaisesRegex(ValueError,'有效SKU'):a.execute('propose_copy',{'reason':'修订','title':'Sprayer 250ml','description':'250ml','evidence_ids':['sku']})
    def test_repeat_and_round_limits(self):
        result=run(self.root,'x-draft.json',self.d,selector([('inspect_sku',{})]*6))
        self.assertEqual(result['state'],'BUDGET_STOP');self.assertEqual(len(result['trace']),6)
        self.assertIn('error',result['trace'][2]['observation'])
    def test_incomplete_output_never_executes(self):
        result=run(self.root,'x-draft.json',self.d,lambda r:{'status':'incomplete','output':[{'type':'function_call','name':'inspect_sku'}]})
        self.assertEqual(result['state'],'INCOMPLETE_RESPONSE');self.assertEqual(result['trace'],[])
    def test_last_turn_candidate_is_checked_without_an_extra_model_call(self):
        result=run(self.root,'x-draft.json',self.d,selector([('inspect_sku',{}),('propose_copy',{'title':'Sprayer 250ml','description':'source-record variant listed as 250ml','evidence_ids':['sku']})]),max_rounds=2)
        self.assertEqual(result['state'],'BUDGET_STOP');self.assertTrue(result['candidate_verified'])
        self.assertTrue(any('内部核查' in x for x in result['checks']['issues']))
        persist(self.root/'drafts/x-draft.json',self.d);persist(job_path(self.root,'x-draft.json'),result)
        checked=verify_stopped_result(self.root,'x-draft.json')
        self.assertFalse(checked['post_run_verification']['feedback_seen_by_model'])
        self.assertEqual(checked['post_run_verification']['additional_model_calls'],0)
    def test_images_never_cross_sku_or_escape_assets(self):
        assets=self.root/'assets';assets.mkdir();(assets/'other.png').write_bytes(b'other');(self.root/'outside.png').write_bytes(b'outside')
        self.d['evidence']['image_review']['source_gallery']=[{'local_file':'assets/other.png'}]
        self.d['evidence']['image_review']['sku_source_galleries']=[{'sku_id':'large','gallery':[{'local_file':'assets/other.png'}]},{'sku_id':'small','gallery':[{'local_file':'../outside.png'}]}]
        a=Audit(self.root,'x-draft.json',self.d);self.assertEqual(len(a.view_images()),1)
        with self.assertRaises(ValueError):a.execute('view_sku_images',{'reason':2})
    def test_candidate_save_preserves_original_and_blocks_stale(self):
        path=self.root/'drafts/x-draft.json';persist(path,self.d)
        result=run(self.root,'x-draft.json',self.d,selector([('inspect_sku',{}),('propose_copy',{'title':'Sprayer 250ml','description':'250ml','evidence_ids':['sku']}),('verify_candidate',{}),('request_human',{'question':'请补充250ml商品实测包装。'})]))
        persist(job_path(self.root,'x-draft.json'),result);saved=save_candidate(self.root,'x-draft.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),self.d);self.assertFalse(saved['human_approved'])
        changed=copy.deepcopy(self.d);changed['payload']['description']='human changed';persist(path,changed)
        with self.assertRaisesRegex(ValueError,'原稿已变化'):save_candidate(self.root,'x-draft.json')
    def test_no_unverified_save_and_no_unconfirmed_model_calls(self):
        result=run(self.root,'x-draft.json',self.d,selector([('inspect_sku',{}),('propose_copy',{'title':'Sprayer 250ml','description':'250ml','evidence_ids':['sku']}),('request_human',{'question':'请补充当前SKU实测包装。'})]))
        result['candidate_verified']=False
        persist(job_path(self.root,'x-draft.json'),result)
        with self.assertRaisesRegex(ValueError,'经过'):save_candidate(self.root,'x-draft.json')
        with self.assertRaisesRegex(ValueError,'明确确认'):start(self.root,'x-draft.json',False)

if __name__=='__main__':unittest.main()
