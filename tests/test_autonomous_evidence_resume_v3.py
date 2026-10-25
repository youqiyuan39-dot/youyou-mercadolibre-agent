import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from src import autonomous_flow_v3 as flow, autonomous_runtime_v3 as runtime
from src import autonomous_evidence_resume_v3 as resume


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.policy = {'enabled': True, 'target': {'store_id':'x','site_id':'MLM'},
                       'authorization': {'reference':'explicit-user', 'publish':True}}
        flow.write(self.root/'config/autonomous_flow.json', self.policy)
        self.name = 'one-draft.json'
        self.capture_file = self.root/'data/intake/first.json'
        flow.write(self.capture_file, {'sku_id':'one','weight_g':None,'captured_at':'old'})
        flow.write(self.root/'drafts'/self.name, {'evidence':{'source_capture':'data/intake/first.json'}})
        signature, _ = resume.source_version(self.root,self.name)
        self.parent = runtime.capture(self.root,self.name,signature)
        self.path = self.root/'data/v3/autonomous-flow'/(self.parent['id']+'.json')
        self.parent.update(status='quarantined',stage='process',reason='NEEDS_INPUT',updated_at=1)
        flow.write(self.path,self.parent)
        self.old_bytes = self.path.read_bytes()

    def changed(self, **fields):
        flow.write(self.root/'data/intake/new.json', {'sku_id':'one','weight_g':113,**fields})
        flow.write(self.root/'drafts'/self.name, {'evidence':{'source_capture':'data/intake/new.json'}})
        signature, _ = resume.source_version(self.root,self.name)
        return signature

    def test_changed_archive_resumes_without_resetting_old_failure(self):
        child = runtime.capture(self.root,self.name,self.changed())
        self.assertNotEqual(child['id'],self.parent['id'])
        self.assertEqual(child['parent_id'],self.parent['id'])
        self.assertEqual(child['stage'],'evidence')
        self.assertEqual(self.path.read_bytes(),self.old_bytes)
        duplicate = runtime.capture(self.root,self.name,child['source_hash'])
        self.assertEqual(duplicate['id'],child['id'])
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),2)

    def test_timestamp_only_or_ai_output_does_not_resume(self):
        flow.write(self.capture_file, {'sku_id':'one','weight_g':None,'captured_at':'new'})
        draft = resume.read(self.root/'drafts'/self.name)
        draft['evidence']['ai_processing_pricing']={'floor_passed':True}
        flow.write(self.root/'drafts'/self.name,draft)
        result=resume.recover_changed(self.root,self.policy)[0]
        self.assertEqual(result['resume_status'],'EVIDENCE_UNCHANGED')
        self.assertEqual(self.path.read_bytes(),self.old_bytes)

    def test_changed_current_policy_cannot_resume(self):
        self.changed()
        changed=copy.deepcopy(self.policy)
        changed['authorization']['reference']='different'
        result=resume.recover_changed(self.root,changed)[0]
        self.assertEqual(result['resume_status'],'RECOVERY_SCOPE_CHANGED')
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),1)

    def test_any_publish_attempt_or_record_requires_reconciliation(self):
        signature=self.changed()
        for marker in ('attempt','record'):
            with self.subTest(marker=marker):
                task=copy.deepcopy(self.parent)
                if marker=='attempt': task['attempts']['publish']=1
                else: flow.write(self.root/'data/v3/publishing/records/x/one-draft.json',{'status':'failed'})
                flow.write(self.path,task)
                result=runtime.capture(self.root,self.name,signature)
                self.assertEqual(result['resume_status'],'RECONCILE_ONLY')
                self.assertEqual(len(list(self.path.parent.glob('*.json'))),1)

    def test_forged_hash_or_outside_archive_cannot_resume(self):
        self.changed()
        with self.assertRaises(ValueError):runtime.capture(self.root,self.name,'f'*64)
        flow.write(self.root/'drafts'/self.name, {'evidence':{'source_capture':'../outside.json'}})
        self.assertEqual(resume.recover_changed(self.root,self.policy)[0]['resume_status'],'SOURCE_UNAVAILABLE')

    def test_source_change_after_resume_blocks_dispatch(self):
        child=runtime.capture(self.root,self.name,self.changed())
        self.changed(weight_g=114)
        adapter=Mock()
        result=runtime.guarded_adapter(self.root,adapter)('evidence',child)
        self.assertEqual(result['reason'],'RECOVERY_SOURCE_CHANGED')
        adapter.assert_not_called()

    def test_corrupt_task_does_not_start_recovery(self):
        (self.path.parent/'corrupt.json').write_text('{',encoding='utf-8')
        self.assertEqual(resume.recover_changed(self.root,self.policy),[{'resume_status':'TASK_RECORD_INVALID'}])
        self.assertEqual(self.path.read_bytes(),self.old_bytes)

    def test_legacy_valid_looking_hash_requires_observation_baseline(self):
        legacy=copy.deepcopy(self.parent)
        legacy.pop('source_version_scheme',None)
        legacy.pop('intake_source_snapshot',None)
        legacy['source_hash']='e'*64
        flow.write(self.path,legacy)
        original=self.path.read_bytes()
        signature,_=resume.source_version(self.root,self.name)
        first=runtime.capture(self.root,self.name,signature)
        self.assertEqual(first['resume_status'],'LEGACY_BASELINE_RECORDED')
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),1)
        second=runtime.capture(self.root,self.name,signature)
        self.assertEqual(second['resume_status'],'EVIDENCE_UNCHANGED')
        self.assertEqual(self.path.read_bytes(),original)
        child=runtime.capture(self.root,self.name,self.changed())
        self.assertEqual(child['parent_id'],legacy['id'])

    def test_worker_recovers_owned_only_and_isolates_missing_archive(self):
        self.changed()
        flow.write(self.root/'drafts/unowned-draft.json',{'evidence':{}})
        child=resume.recover_changed(self.root,self.policy)[0]
        calls=[]
        def adapter(stage,task):
            calls.append(stage)
            return {'outcome':'blocked','reason':'OFFICIAL_QUOTE_MISSING'} if stage=='validate' else {'outcome':'passed'}
        for moment in (1,2,3):flow.advance(self.root,child['id'],adapter,now=moment)
        result=resume.read(self.path.parent/(child['id']+'.json'))
        self.assertEqual(result['status'],'quarantined')
        self.assertEqual(calls,['evidence','process','validate'])
        self.assertNotIn('publish',calls)


if __name__=='__main__':unittest.main()
