import json
import tempfile
import unittest
from pathlib import Path
from src import autonomous_flow_v3 as flow


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def new(self, name='one-draft.json'):
        return flow.enqueue(self.root, name, 'snapshot', {'store_id': 'bound', 'site_id': 'MLM'},
                            {'reference': 'explicit-user-workflow-authorization'})

    def test_dedup_does_not_reset_progress(self):
        task = self.new()
        flow.advance(self.root, task['id'], lambda *_: {'outcome': 'passed'}, now=1)
        self.assertEqual(self.new()['stage'], 'process')

    def test_publish_timeout_reconciles_without_reposting(self):
        task = self.new(); calls = []
        def adapter(stage, task):
            calls.append(stage)
            if stage == 'publish': raise TimeoutError()
            if stage == 'reconcile':
                return {'outcome': 'passed', 'artifact': {'item_id': 'MLM_TEST', 'verified_live': True}}
            return {'outcome': 'passed'}
        for moment in (1, 2, 3, 4, 5, 30):
            task = flow.advance(self.root, task['id'], adapter, now=moment)
        self.assertEqual(task['status'], 'complete')
        self.assertEqual(calls.count('publish'), 1)

    def test_crash_before_receipt_does_not_create_again(self):
        task = self.new(); task.update(stage='publish', status='dispatching')
        flow.write(self.root/'data/v3/autonomous-flow'/(task['id']+'.json'), task)
        seen = []
        flow.advance(self.root, task['id'], lambda stage, _: seen.append(stage) or {'outcome': 'unknown'}, now=1)
        self.assertEqual(seen, ['reconcile'])

    def test_blocked_product_does_not_stop_next_product(self):
        self.new('bad-draft.json'); self.new('good-draft.json')
        rows = flow.sweep(self.root, lambda _, t: {'outcome': 'blocked' if t['draft'].startswith('bad') else 'passed'}, now=1)
        self.assertEqual({r['status'] for r in rows}, {'quarantined', 'pending'})

    def test_missing_live_receipt_is_not_complete(self):
        task=self.new(); task.update(stage='reconcile')
        flow.write(self.root/'data/v3/autonomous-flow'/(task['id']+'.json'), task)
        result=flow.advance(self.root,task['id'],lambda *_:{'outcome':'passed'},now=1)
        self.assertEqual(result['reason'],'LIVE_RECEIPT_MISSING')

    def test_long_running_job_waits_without_exhausting_retries(self):
        task=self.new()
        for moment in range(0, 1000, 20):
            task=flow.advance(self.root,task['id'],lambda *_:{'outcome':'wait'},now=moment)
        self.assertEqual(task['status'],'pending')
        self.assertEqual(task['attempts']['evidence'],0)


if __name__ == '__main__': unittest.main()
