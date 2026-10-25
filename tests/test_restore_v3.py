import hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from tools import portable_data as p
from src.local_model_runtime import profile,runtime_args,LocalModelError

class RestoreTests(unittest.TestCase):
    def test_model_relative_paths_do_not_depend_on_cwd_or_user(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);config=profile(root)
            self.assertEqual(Path(config['model_file']),root/'models/Qwen3.5-9B-Q4_K_M.gguf')
            (root/'models').mkdir();(root/'models/mmproj-F16.gguf').write_bytes(b'test fixture')
            args=runtime_args(config)
            self.assertEqual(args[args.index('--device')+1],'none')
            (root/'config').mkdir();(root/'config/local_model_runtime.json').write_text(json.dumps({'model_file':'../outside.gguf'}))
            with self.assertRaises(LocalModelError):profile(root)

    def test_browser_data_rejected_and_import_rebases_only_project_paths(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);old=base/'old';new=base/'new';(old/'drafts').mkdir(parents=True);new.mkdir()
            data={'asset':str(old/'assets/a.png'),'remote':'https://example.com/a.png'}
            (old/'drafts/a.json').write_text(json.dumps(data))
            browser=old/'data/v3/desktop-webview-v2/Default';browser.mkdir(parents=True)
            (browser/'Preferences').write_text('must not read or copy')
            z=base/'backup.zip';self.assertEqual(p.export_data(z,old),1)
            self.assertEqual(p.import_data(z,new),1)
            restored=json.loads((new/'drafts/a.json').read_text(encoding='utf-8'))
            self.assertEqual(restored['asset'],(new/'assets/a.png').as_posix())
            self.assertEqual(restored['remote'],data['remote'])
            self.assertTrue((new/'data/MIGRATION_REVIEW_REQUIRED.json').exists())
            self.assertFalse((new/'data/v3/desktop-webview-v2').exists())

    def test_corrupt_or_case_duplicate_archive_has_no_partial_business_write(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);root=base/'new';root.mkdir()
            for duplicate in (False,True):
                z=base/('bad'+str(duplicate)+'.zip')
                with zipfile.ZipFile(z,'w') as a:
                    a.writestr('drafts/a.json','{}')
                    if duplicate:a.writestr('DRAFTS/A.JSON','{}')
                    a.writestr(p.MANIFEST,json.dumps({'version':2,'source_root':'old','files':[{'path':'drafts/a.json','size':2,'sha256':'wrong'}]}))
                with self.assertRaises(ValueError):p.import_data(z,root)
                self.assertFalse((root/'drafts/a.json').exists())
                self.assertFalse((root/'data/MIGRATION_REVIEW_REQUIRED.json').exists())

    def test_sensitive_and_process_paths_are_not_exportable(self):
        for path in ['data/v3/desktop-webview/Default/Cookies','data/v3/server.pid','.secrets/a.dat','data/operations/orders.json','data/orders_snapshot.json','assets/ui/logo.png','data/test-scratch/test.json']:
            self.assertFalse(p.allowed(path),path)

if __name__=='__main__':unittest.main()
