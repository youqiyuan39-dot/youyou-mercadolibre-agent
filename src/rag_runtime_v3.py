"""Workbench-owned retrieval runtime; stdlib-only in the desktop executable."""
import json,os,subprocess
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request

def health(root):
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:18081/health',timeout=.5) as response:data=json.load(response)
        return data if data.get('ok') and Path(data.get('workspace','')).resolve()==Path(root).resolve() else None
    except (OSError,ValueError):return None

def start(root):
    root=Path(root);old=health(root)
    if old:return None
    python=root/'tools/rag_env/Scripts/python.exe'
    if not python.is_file() or not (root/'tools/rag_models').is_dir():return None
    log=root/'data/v3/rag-runtime.log';log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('ab') as output:
        return subprocess.Popen([str(python),'-m','src.rag_embedding_server_v3','--root',str(root),'--parent',str(os.getpid())],cwd=root,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:pass

def search(root,query,digest):
    request=Request('http://127.0.0.1:18081/search',data=json.dumps({'query':query},ensure_ascii=False).encode(),headers={'Content-Type':'application/json','X-Yoyo-Rag':'1'},method='POST')
    try:
        with build_opener(ProxyHandler({})).open(request,timeout=3) as response:data=json.load(response)
        if data.get('ok') and data.get('corpus_digest')==digest:return data
    except (OSError,ValueError):pass
    return None
