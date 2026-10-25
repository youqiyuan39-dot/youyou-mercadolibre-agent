"""Temporary loopback preparation page. Does not start any product/AI worker."""
import argparse
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from . import supplier1688_routes as routes

ROOT=Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def _json(self,status,value):
        body=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def _file(self,path,mime):
        body=path.read_bytes();self.send_response(200);self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def do_GET(self):
        path=urlparse(self.path).path
        if routes.get(self,ROOT,path):return
        if path=='/workbench_v3.css':return self._file(ROOT/'web/workbench_v3.css','text/css; charset=utf-8')
        self._json(404,{'ok':False,'error':'只提供1688接入准备页'})
    def do_POST(self):
        if not routes.post(self,ROOT,urlparse(self.path).path):self._json(404,{'ok':False,'error':'未接入'})
    def log_message(self,*args):return


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8792);args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.supplier_setup_only=True
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
