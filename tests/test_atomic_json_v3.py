import ctypes,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from src.atomic_json_v3 import write

class AtomicTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='win32','Actual Windows share/delete behavior')
    def test_windows_open_reader_recovers_without_losing_saved_candidate(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'draft.json';p.write_text('{"old":1}','utf-8')
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
            kernel.CreateFileW.restype=ctypes.c_void_p;kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.CreateFileW(str(p),0x80000000,1,None,3,128,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            release=threading.Thread(target=lambda:(time.sleep(.10),kernel.CloseHandle(handle)));release.start()
            write(p,{'saved':2});release.join();self.assertEqual(json.loads(p.read_text('utf-8')),{'saved':2})
    def test_retry_does_not_overwrite_a_concurrent_human_change(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'draft.json';p.write_text('{"old":1}','utf-8')
            def reader_delay(_):p.write_text('{"human":9}','utf-8')
            with patch.object(Path,'replace',side_effect=PermissionError('shared reader')),patch('src.atomic_json_v3.time.sleep',side_effect=reader_delay):
                with self.assertRaisesRegex(RuntimeError,'未覆盖'):write(p,{'AI':2})
            self.assertEqual(json.loads(p.read_text('utf-8')),{'human':9})
