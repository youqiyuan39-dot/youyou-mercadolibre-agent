import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from src import autonomous_flow_v3 as flow, autonomous_runtime_v3 as runtime


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.policy={'enabled':True,'target':{'store_id':'x','site_id':'MLM'},
                     'authorization':{'reference':'user','publish':True}}
        flow.write(self.root/'config/autonomous_flow.json',self.policy)
        self.task=runtime.capture(self.root,'one-draft.json','source')
        self.path=self.root/'data/v3/autonomous-flow'/(self.task['id']+'.json')

    def test_wait_timeout_survives_restart(self):
        self.task.update(stage='process');flow.write(self.path,self.task)
        waiting=Mock(return_value={'outcome':'wait','artifact':{'started':True}})
        flow.advance(self.root,self.task['id'],waiting,now=0)
        result=flow.advance(self.root,self.task['id'],waiting,now=7200)
        self.assertEqual(result['reason'],'STAGE_TIMEOUT:process')
        self.assertEqual(waiting.call_count,1)

    def test_unknown_processing_dispatch_does_not_start_again(self):
        self.task.update(stage='process',status='running');flow.write(self.path,self.task)
        call=Mock()
        result=flow.advance(self.root,self.task['id'],call,now=1)
        self.assertEqual(result['reason'],'PROCESS_DISPATCH_UNKNOWN');call.assert_not_called()

    def test_publish_wait_never_posts_twice(self):
        self.task.update(stage='publish');flow.write(self.path,self.task)
        call=Mock(return_value={'outcome':'wait'})
        flow.advance(self.root,self.task['id'],call,now=0)
        flow.advance(self.root,self.task['id'],call,now=30)
        self.assertEqual([c.args[0] for c in call.call_args_list],['publish','reconcile'])

    def test_unresolved_publication_times_out_without_new_submit(self):
        self.task.update(stage='reconcile');flow.write(self.path,self.task)
        call=Mock(return_value={'outcome':'wait'})
        flow.advance(self.root,self.task['id'],call,now=0)
        result=flow.advance(self.root,self.task['id'],call,now=86400)
        self.assertEqual(result['reason'],'PUBLICATION_OUTCOME_UNRESOLVED')
        self.assertEqual(call.call_count,1)

    def test_event_history_is_bounded(self):
        self.task.update(stage='process');flow.write(self.path,self.task)
        for t in range(300):
            result=flow.advance(self.root,self.task['id'],lambda *_:{'outcome':'wait','retry_after':1},now=t)
        self.assertEqual(len(result['events']),200)
        self.assertEqual(result['events_pruned'],100)

    def test_revoked_or_changed_policy_never_dispatches(self):
        for change in ({'enabled':False},{'target':{'store_id':'y','site_id':'MLM'}},
                       {'authorization':{'reference':'user','publish':False}}):
            flow.write(self.root/'config/autonomous_flow.json',{**self.policy,**change})
            call=Mock();guard=runtime.guarded_adapter(self.root,call)
            self.assertEqual(guard('publish',self.task)['outcome'],'blocked')
            call.assert_not_called()

    def test_revocation_still_allows_readback_of_prior_submission(self):
        flow.write(self.root/'config/autonomous_flow.json',{'enabled':False})
        call=Mock(return_value={'outcome':'wait'})
        self.assertEqual(runtime.guarded_adapter(self.root,call)('reconcile',self.task)['outcome'],'wait')
        call.assert_called_once()

    def test_enabled_matching_policy_dispatches(self):
        call=Mock(return_value={'outcome':'passed'})
        runtime.guarded_adapter(self.root,call)('publish',self.task)
        call.assert_called_once()

if __name__=='__main__': unittest.main()
