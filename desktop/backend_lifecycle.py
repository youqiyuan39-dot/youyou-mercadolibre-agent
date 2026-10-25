"""A desktop window explicitly owns the matching local workbench lifetime."""
import json,time,threading
from pathlib import Path
from urllib.request import Request,ProxyHandler,build_opener

class BackendLifetime:
    def __init__(self,root,url,health,process=None,*,mode='window_bound'):
        if mode not in ('window_bound','resident'):raise ValueError('Unknown backend lifetime mode')
        self.mode=mode
        self.root=Path(root).resolve();self.url=url;self.health=health;self.process=process;self.closed=False;self.lock=threading.Lock()
    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        # Resident windows are clients only. Neither a close event nor GUI
        # initialization failure may stop the independently managed backend.
        if self.mode=='resident':return
        state=self.health()
        if state:
            if Path(state.get('workspace','')).resolve()!=self.root:raise RuntimeError('后台工作区已变化，未关闭其他服务')
            if state.get('desktop_lifecycle')!='WINDOW_BOUND_V1':raise RuntimeError('旧后台不支持随窗口退出，需先退出旧后台一次')
            opener=build_opener(ProxyHandler({}))
            req=Request(self.url+'/api/v3/desktop/shutdown',data=b'{"confirmed":true}',headers={'Content-Type':'application/json'},method='POST')
            try:
                with opener.open(req,timeout=3) as response:
                    if not json.load(response).get('ok'):raise RuntimeError('后台未接受退出请求')
            except OSError:
                if self.process is None:raise RuntimeError('后台退出请求失败，未结束其他进程')
        elif self.process is None:return
        for _ in range(30):
            if self.process is not None:
                if self.process.poll() is not None:return
            elif not self.health():return
            time.sleep(.1)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=3)
        elif self.health():raise RuntimeError('后台未在等待时间内退出，未结束其他进程')
