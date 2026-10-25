import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src import draft_editor as core

class PublishRowDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.drafts=self.root/'drafts';self.drafts.mkdir()
        self.first=self.drafts/'first-draft.json';self.first.write_text('{"payload":{"title":"first"}}','utf-8')
        self.second=self.drafts/'second-draft.json';self.second.write_text('{"payload":{"title":"second"}}','utf-8')
    def save(self,relative,value):
        p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value),'utf-8');return p
    def recycle(self,*paths):return core.batch_draft_action('recycle',[p.name for p in paths],self.drafts,self.root)
    def test_local_delete_is_byte_recoverable_and_keeps_raw_images_and_receipt(self):
        original=self.first.read_bytes();raw=self.save('data/intake/raw.json',{'original':True})
        receipt=self.save('data/v3/publishing/records/store/first-draft.json',{'status':'success'})
        asset=self.root/'assets/photo.jpg';asset.parent.mkdir();asset.write_bytes(b'original-photo')
        before={p:p.read_bytes() for p in [raw,receipt,asset]}
        result=self.recycle(self.first)['results'][0];archived=Path(result['restore_path'])
        self.assertFalse(self.first.exists());self.assertEqual(archived.read_bytes(),original)
        self.assertEqual({p:p.read_bytes() for p in before},before)
        archived.replace(self.first);self.assertEqual(self.first.read_bytes(),original)
    def test_queued_job_blocks_entire_batch_before_any_file_is_moved(self):
        self.save('jobs/second-draft-image-job.json',{'status':'queued'})
        with self.assertRaisesRegex(core.EditorError,'任务'):self.recycle(self.first,self.second)
        self.assertTrue(self.first.exists());self.assertTrue(self.second.exists())
    def test_pending_auto_processing_and_unresolved_publish_are_not_deleted(self):
        queue=self.save('data/v3/auto-processing/first-draft.json',{'status':'pending'})
        with self.assertRaises(core.EditorError):self.recycle(self.first)
        queue.write_text('{"status":"done"}','utf-8')
        self.save('data/v3/publishing/records/store/first-draft.json',{'status':'partial','variants':[{'sites':[{'status':'pending_confirmation'}]}]})
        with self.assertRaises(core.EditorError):self.recycle(self.first)
        self.assertTrue(self.first.exists())
    def test_repeated_filename_keeps_both_deleted_versions(self):
        first=Path(self.recycle(self.first)['results'][0]['restore_path'])
        self.first.write_text('{"new_version":true}','utf-8')
        second=Path(self.recycle(self.first,self.first)['results'][0]['restore_path'])
        self.assertNotEqual(first,second);self.assertIn('payload',json.loads(first.read_text('utf-8')))
        self.assertTrue(json.loads(second.read_text('utf-8'))['new_version'])
    def test_changed_draft_is_not_deleted(self):
        def edit(*_):self.first.write_text('{"human_edit":true}','utf-8')
        with patch.object(core,'_check_recycle_available',side_effect=edit),self.assertRaisesRegex(core.EditorError,'其他操作修改'):self.recycle(self.first)
        self.assertTrue(json.loads(self.first.read_text('utf-8'))['human_edit'])

if __name__=='__main__':unittest.main()
