"""Workbench-owned autonomous worker. Only explicitly enqueued captures run."""
import json
import logging
import threading
import time
from pathlib import Path
from . import autonomous_flow_v3 as flow
from .autonomous_workbench_adapter import WorkbenchAdapter


def policy(root):
    path=Path(root)/'config/autonomous_flow.json'
    value=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if value.get('enabled') is not True:
        return None
    if not value.get('target',{}).get('store_id') or value.get('target',{}).get('site_id')!='MLM':
        raise ValueError('Explicit supported autonomous target required')
    if not value.get('authorization',{}).get('reference'):
        raise ValueError('Autonomous authorization reference required')
    return value


def capture(root, name, source_hash):
    active=policy(root)
    if not active:return None
    from .autonomous_evidence_resume_v3 import capture as resume_capture
    return resume_capture(root,name,source_hash,active['target'],active['authorization'])


def guarded_adapter(root, adapter):
    def dispatch(stage, task):
        # Reconciliation reads previously submitted results even after an
        # authorization revocation; it must never create a new listing.
        if stage == 'reconcile':
            return adapter(stage, task)
        try:
            active=policy(root)
        except (ValueError, OSError, TypeError):
            return {'outcome':'blocked','reason':'AUTONOMOUS_POLICY_INVALID'}
        if not active:
            return {'outcome':'blocked','reason':'AUTONOMOUS_DISABLED'}
        if active['target'] != task['target']:
            return {'outcome':'blocked','reason':'AUTONOMOUS_TARGET_CHANGED'}
        if active['authorization'] != task['authorization']:
            return {'outcome':'blocked','reason':'AUTONOMOUS_AUTHORIZATION_CHANGED'}
        if stage == 'publish' and active['authorization'].get('publish') is not True:
            return {'outcome':'blocked','reason':'PUBLICATION_NOT_AUTHORIZED'}
        if task.get('recovery_source_snapshot'):
            from .autonomous_evidence_resume_v3 import source_version
            try:
                version, snapshot = source_version(root, task['draft'])
                if version != task['source_hash'] or snapshot != task['recovery_source_snapshot']:
                    return {'outcome':'blocked','reason':'RECOVERY_SOURCE_CHANGED'}
            except (ValueError, OSError):
                return {'outcome':'blocked','reason':'RECOVERY_SOURCE_UNAVAILABLE'}
        return adapter(stage, task)
    return dispatch


def start(root, legacy, stopped):
    adapter=guarded_adapter(root,WorkbenchAdapter(root,legacy))
    def run():
        next_recovery = 0
        while not stopped.wait(2):
            try:
                if time.monotonic() >= next_recovery:
                    active = policy(root)
                    if active:
                        from .autonomous_evidence_resume_v3 import recover_changed
                        recover_changed(root, active)
                    next_recovery = time.monotonic() + 30
                flow.sweep(root,adapter)
            except Exception:
                logging.exception('Autonomous coordinator failed; retained task state')
    worker=threading.Thread(target=run,name='autonomous-workbench',daemon=True)
    worker.start()
    return worker
