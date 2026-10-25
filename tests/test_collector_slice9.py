import json, shutil, tempfile, unittest
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from src.collector_receiver import ensure_receiver
from src.draft_editor import YOUYOU_EXTENSION_ORIGIN

class CollectorCors(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config').mkdir()
        self.extra='chrome-extension://'+'a'*32
        (self.root/'config/collector_allowed_origins.json').write_text(json.dumps({'allowed_origins':[self.extra,'https://evil.invalid','chrome-extension://*']}),encoding='utf-8')
        self.server,_=ensure_receiver(self.root,0);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def request(self,origin,method='OPTIONS',data=None):
        req=Request(self.url+'/api/market-reference',data=data,method=method,headers={'Origin':origin,'X-Youyou-Collector':'1','Access-Control-Request-Private-Network':'true'})
        try:r=build_opener(ProxyHandler({})).open(req)
        except HTTPError as e:r=e
        with r:return r.status,dict(r.headers),r.read()
    def test_production_and_injected_preflight(self):
        for origin in (YOUYOU_EXTENSION_ORIGIN,self.extra):
            code,h,_=self.request(origin);self.assertEqual(code,204)
            self.assertEqual(h['Access-Control-Allow-Origin'],origin);self.assertEqual(h['Access-Control-Allow-Private-Network'],'true')
            self.assertIn('X-Youyou-Collector',h['Access-Control-Allow-Headers'])
    def test_unknown_origin_denied(self):
        for origin in ('https://evil.invalid','chrome-extension://'+'b'*32):
            code,h,_=self.request(origin);self.assertEqual(code,403);self.assertNotIn('Access-Control-Allow-Origin',h)
    def test_injected_origin_post_and_size_error_visible(self):
        code,h,body=self.request(self.extra,'POST',b'x'*131073);self.assertEqual(code,400)
        self.assertEqual(h['Access-Control-Allow-Origin'],self.extra);self.assertIn('request_size',body.decode())
    def test_default_only_production(self):
        from src.collector_receiver import make_collector_handler
        other=Path(self.tmp.name)/'default';other.mkdir()
        from http.server import ThreadingHTTPServer
        import threading
        server=ThreadingHTTPServer(('127.0.0.1',0),make_collector_handler(other));threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown);self.url=f'http://127.0.0.1:{server.server_port}'
        self.assertEqual(self.request(self.extra)[0],403);self.assertEqual(self.request(YOUYOU_EXTENSION_ORIGIN)[0],204)

    def test_injected_origin_collect_route_keeps_parent_validation(self):
        req=Request(self.url+'/api/collect',data=b'{}',headers={'Origin':self.extra,'X-Youyou-Collector':'1','Content-Type':'application/json'})
        try:response=build_opener(ProxyHandler({})).open(req)
        except HTTPError as e:response=e
        with response:
            self.assertEqual(response.status,400)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],self.extra)
        self.assertFalse((self.root/'drafts').exists())
