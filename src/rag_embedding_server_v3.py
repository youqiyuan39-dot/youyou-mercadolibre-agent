"""CPU-only local semantic retrieval sidecar; no chat model and no remote calls."""
import argparse,ctypes,hashlib,json,os,re,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from collections import OrderedDict

MODEL='BAAI/bge-small-zh-v1.5'
def parent_alive(pid):
    if os.name!='nt':return os.getppid()==pid
    api=ctypes.windll.kernel32;api.OpenProcess.restype=ctypes.c_void_p
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:return False
    value=ctypes.c_ulong();api.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    api.CloseHandle.argtypes=[ctypes.c_void_p]
    try:return bool(api.GetExitCodeProcess(handle,ctypes.byref(value))) and value.value==259
    finally:api.CloseHandle(handle)

class Index:
    def __init__(self,root):
        from fastembed import TextEmbedding
        self.root=root;self.lock=threading.Lock();self.digest='';self.cache=OrderedDict()
        self.model=TextEmbedding(MODEL,cache_dir=str(root/'tools/rag_models'),threads=2,providers=['CPUExecutionProvider'],local_files_only=True)
        self.refresh()
    def refresh(self):
        import numpy as np
        raw=(self.root/'knowledge/project/knowledge.json').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if digest==self.digest:return
        docs=json.loads(raw)['documents'];chunks=[]
        for d in docs:
            if d.get('status')!='ACTIVE':continue
            text=d['content'];parts=[text[i:i+260] for i in range(0,len(text),260)]
            for part in parts:
                body=d['title']+'\n'+d['keywords']+'\n'+part
                chunks.append({'id':d['id'],'text':body,'key':hashlib.sha256((MODEL+body).encode()).hexdigest()})
        path=self.root/'knowledge/project/semantic-index.json'
        old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        vectors=old.get('vectors') or {} if old.get('model')==MODEL else {}
        missing=[c for c in chunks if c['key'] not in vectors]
        if missing:
            for c,v in zip(missing,self.model.embed([c['text'] for c in missing],batch_size=8)):
                vectors[c['key']]=v.tolist()
        vectors={c['key']:vectors[c['key']] for c in chunks}
        path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps({'model':MODEL,'corpus_digest':digest,'vectors':vectors},separators=(',',':')),encoding='utf-8');temp.replace(path)
        self.chunks=chunks;self.matrix=np.asarray([vectors[c['key']] for c in chunks],dtype=np.float32)
        self.matrix/=np.maximum(np.linalg.norm(self.matrix,axis=1,keepdims=True),1e-9)
        self.digest=digest;self.cache.clear()
    def search(self,query):
        import numpy as np
        with self.lock:
            self.refresh()
            if query in self.cache:return {**self.cache[query],'query_cache_hit':True}
            start=time.monotonic();v=np.asarray(next(iter(self.model.query_embed([query]))),dtype=np.float32);v/=max(float(np.linalg.norm(v)),1e-9)
            scores=self.matrix@v;by_id={}
            for c,s in zip(self.chunks,scores):by_id[c['id']]=max(float(s),by_id.get(c['id'],-1))
            ranked=sorted(by_id.items(),key=lambda x:(-x[1],x[0]))[:10]
            result={'model':MODEL,'corpus_digest':self.digest,'items':[{'id':k,'cosine':round(s,6)} for k,s in ranked],'embedding_seconds':round(time.monotonic()-start,4),'query_cache_hit':False}
            self.cache[query]=result
            if len(self.cache)>128:self.cache.popitem(last=False)
            return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--parent',type=int,required=True);parser.add_argument('--port',type=int,default=18081);args=parser.parse_args()
    root=Path(args.root).resolve();index=Index(root)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,data):
            body=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path!='/health':return self.reply(404,{'ok':False})
            self.reply(200,{'ok':True,'workspace':str(root),'parent_pid':args.parent,'pid':os.getpid(),'model':MODEL,'provider':'CPUExecutionProvider','chunks':len(index.chunks),'corpus_digest':index.digest})
        def do_POST(self):
            if self.path!='/search' or self.headers.get('Origin') or self.headers.get('X-Yoyo-Rag')!='1':return self.reply(403,{'ok':False})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<12000:raise ValueError('invalid size')
                query=json.loads(self.rfile.read(size)).get('query')
                if not isinstance(query,str) or not 1<=len(query)<=2000:raise ValueError('invalid query')
                self.reply(200,{'ok':True,**index.search(query)})
            except Exception as exc:self.reply(400,{'ok':False,'error':type(exc).__name__})
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.daemon_threads=True
    def watch():
        while parent_alive(args.parent):time.sleep(1)
        server.shutdown()
    threading.Thread(target=watch,daemon=True).start()
    try:server.serve_forever()
    finally:server.server_close()
if __name__=='__main__':main()
