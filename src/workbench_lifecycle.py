"""Cooperative workbench exit; leaves unrelated services and shared models alone."""
import json,threading
from pathlib import Path
from datetime import datetime,timezone
VERSION='WINDOW_BOUND_V1'

def pause_jobs(root):
    from .workbench_assistant import write
    root=Path(root);paused=[]
    for pattern in ('*-full-ai-job.json','*-image-job.json','*-sku-image-job.json'):
        for path in (root/'jobs').glob(pattern):
            if not path.resolve().is_relative_to((root/'jobs').resolve()):continue
            try:
                job=json.loads(path.read_text(encoding='utf-8'))
                if job.get('status') not in ('queued','running'):continue
                job.update(status='paused',message='工作台窗口已关闭，任务暂停；重新打开后可检查并重试',pause_reason='WORKBENCH_CLOSED',updated_at=datetime.now(timezone.utc).isoformat())
                for item in job.get('items') or []:
                    if item.get('status') in ('queued','generating','running'):item['status']='paused'
                write(path,job);paused.append(path.name)
            except (OSError,ValueError,TypeError):continue
    return paused

def bind(server,root):
    stopped=threading.Event()
    def request_stop():
        if stopped.is_set():return
        stopped.set()
        def stop():
            from . import workbench_assistant as assistant
            try:
                pause_jobs(root)
                assistant.stop_owned_workers(root)
            finally:server.shutdown()
        threading.Thread(target=stop,name='workbench-shutdown',daemon=True).start()
    server.request_workbench_stop=request_stop
    return stopped
