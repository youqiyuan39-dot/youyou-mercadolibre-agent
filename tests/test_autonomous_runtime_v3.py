import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import autonomous_runtime_v3 as runtime, autonomous_flow_v3 as flow
from src import auto_processing_v3 as queue


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def test_disabled_does_not_enroll(self):
        self.assertIsNone(runtime.capture(self.root,'one-draft.json','hash'))

    def test_capture_goes_to_one_owner_only(self):
        flow.write(self.root/'config/autonomous_flow.json',{'enabled':True,'target':{'store_id':'test','site_id':'MLM'},'authorization':{'reference':'user'}})
        result=queue.enqueue(self.root,self.root/'drafts/one-draft.json',{'offer_id':'one'})
        self.assertIn('autonomous_task_id',result)
        self.assertFalse((self.root/'data/v3/auto-processing/one-draft.json').exists())
        self.assertEqual(queue.enqueue(self.root,self.root/'drafts/one-draft.json',{'offer_id':'one'})['autonomous_task_id'],result['autonomous_task_id'])

    def test_worker_obeys_shutdown(self):
        stopped=threading.Event();stopped.set()
        worker=runtime.start(self.root,Mock(),stopped);worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__=='__main__':unittest.main()
