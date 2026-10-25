"""Durable intake queue. Only new collection events enqueue; never scan old drafts."""
import hashlib
import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

LOCK = threading.RLock()
WAKE = threading.Event()


def write(path, value):
    from .atomic_json_v3 import write as atomic_write
    atomic_write(path,value)


def capture_facts(capture):
    content=copy.deepcopy({k:v for k,v in capture.items() if k not in {'captured_at','collected_at'}})
    if isinstance(content.get('api_evidence'),dict):content['api_evidence'].pop('observed_at',None)
    return content


def input_requests(items, evidence_issues=()):
    """Consolidate the same missing input across SKUs without losing their scope."""
    issues=list(evidence_issues or [])
    for item in items or []:
        for issue in item.get('issues') or []:issues.append({**issue,'sku_id':item.get('sku_id')})
        if item.get('status')=='failed' and item.get('error'):
            issues.append({'sku_id':item.get('sku_id'),'cause':'GENERATION_OR_VALIDATION_FAILED','field':'ai_content',
                           'required_evidence':'核对生成/保存失败反馈并从检查点重试；不要求重填全部商品事实：'+str(item['error'])[:500]})
    grouped={}
    for issue in issues:
        text=issue.get('required_evidence') or issue.get('reason') or ''
        code=issue.get('code') or issue.get('cause') or 'NEEDS_INPUT'
        # Keep known contradictory values: two different pack counts must not
        # be collapsed into an instruction which conceals the difference.
        values={k:issue.get(k) for k in ('supplier_value','reference_value') if k in issue}
        key=json.dumps([code,issue.get('field'),text,values],sort_keys=True,ensure_ascii=False)
        row=grouped.setdefault(key,{'code':code,'field':issue.get('field'),'request':text,'sku_ids':[],**values})
        sid=issue.get('sku_id')
        if sid is not None and str(sid) not in row['sku_ids']:row['sku_ids'].append(str(sid))
    return list(grouped.values())


def enqueue(root, path, capture):
    root, path = Path(root), Path(path)
    if publication_present(root, path.name):
        return {'status': 'SKIPPED_PUBLISHED'}
    content = capture_facts(capture)
    signature = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    from .autonomous_runtime_v3 import capture
    coordinated = capture(root, path.name, signature)
    if coordinated is not None:
        return {'status': coordinated['status'], 'autonomous_task_id': coordinated['id'],
                'stage': coordinated['stage']}
    destination = root / 'data' / 'v3' / 'auto-processing' / (path.stem + '.json')
    with LOCK:
        old = json.loads(destination.read_text(encoding='utf-8')) if destination.exists() else {}
        if old.get('signature') == signature:
            return {'status': old.get('status'), 'duplicate': True}
        value = {'draft': path.name, 'signature': signature, 'status': 'pending', 'paid_api_used': False}
        write(destination, value)
    WAKE.set()
    return {'status': 'pending', 'duplicate': False}


def publication_present(root, name):
    filename = Path(name).stem + '.json'
    records = [root / 'data' / 'publish_records' / filename] + list((root / 'data' / 'v3' / 'publishing' / 'records').glob('*/' + filename))
    return any(p.is_file() and json.loads(p.read_text(encoding='utf-8')).get('status') in {'success', 'partial', 'pending_confirmation', 'submitting'} for p in records)


def dispatch_once(root, legacy, stopped):
    for path in sorted((root / 'data' / 'v3' / 'auto-processing').glob('*.json')):
        if stopped.is_set(): break
        with LOCK:
            try:entry = json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue
        if not isinstance(entry,dict) or entry.get('status') not in {'pending', 'running'}: continue
        if not isinstance(entry.get('draft'),str) or Path(entry['draft']).name!=entry['draft'] or not entry['draft'].endswith('-draft.json'):continue
        name = entry['draft']
        if publication_present(root, name):
            entry['status'] = 'SKIPPED_PUBLISHED'
            with LOCK: write(path, entry)
            continue
        job_path = root / 'jobs' / (Path(name).stem + '-full-ai-job.json')
        try:job = json.loads(job_path.read_text(encoding='utf-8')) if job_path.exists() else {}
        except (OSError,ValueError):job={'status':'failed','error':'任务记录无法解析；原件保留，请修复记录后重试'}
        if not isinstance(job,dict):job={'status':'failed','error':'任务记录格式无效；原件保留'}
        if entry['status'] == 'running':
            if job.get('status') in {'queued', 'running'}: continue
            completion=job.get('completion_state')
            transport=job.get('status') or 'failed'
            entry.update(status=('done' if completion=='READY_FOR_FINAL_REVIEW' else 'needs_attention') if transport=='done' else transport,
                         job_status=transport,completion_state=completion,error=job.get('error'),
                         message=job.get('message'),items=job.get('items') or [],
                         input_requests=job.get('input_requests') or input_requests(job.get('items')),
                         finished_at=datetime.now(timezone.utc).isoformat(),
                         next_action='REVIEW_CANDIDATE' if completion=='READY_FOR_FINAL_REVIEW' else 'FIX_RECORDED_INPUTS_AND_RESUME')
        else:
            # Re-collection while a task is active stays pending, never overwrites it.
            if job.get('status') in {'queued', 'running'}: continue
            try:
                legacy.start_full_ai_flow(name, root / 'drafts', root)
                entry['status'] = 'running'
            except Exception as exc:
                entry.update(status='failed', error=str(exc)[:600])
        with LOCK:
            latest = json.loads(path.read_text(encoding='utf-8'))
            if latest.get('signature') == entry.get('signature'): write(path, entry)


def start_worker(root, legacy, stopped):
    recover_interrupted(root)
    def worker():
        while not stopped.is_set():
            try: dispatch_once(root, legacy, stopped)
            except Exception:
                # A malformed queue entry must not terminate the desktop backend.
                import logging
                logging.exception('Auto processing queue failed')
            WAKE.wait(1); WAKE.clear()
    thread = threading.Thread(target=worker, name='intake-local-ai', daemon=True)
    thread.start()
    return thread

def recover_interrupted(root):
    """Only entries owned by the intake queue recover; never scan old drafts into it."""
    root=Path(root)
    for path in (root/'data/v3/auto-processing').glob('*.json'):
        try:
            entry=json.loads(path.read_text(encoding='utf-8'))
            if entry.get('status')!='running' or publication_present(root,entry['draft']):continue
            job_path=root/'jobs'/(Path(entry['draft']).stem+'-full-ai-job.json')
            job=json.loads(job_path.read_text(encoding='utf-8')) if job_path.exists() else {}
            if job.get('status') not in ('queued','running'):continue
            count=int(entry.get('recovery_count',0))+1
            job.update(status='interrupted',message='工作台重启，已保存进度；只恢复本队列任务')
            write(job_path,job)
            entry.update(status='pending' if count<=1 else 'needs_attention',recovery_count=count)
            write(path,entry)
        except (ValueError,OSError,KeyError):continue

def editable_inputs(draft):
    ev=draft.get('evidence') or {}
    return {'skus':[{k:s.get(k) for k in ('sku_id','purchase_cost_cny','domestic_shipping_cny','package','market_reference')}
                    | {'sites':[{k:site.get(k) for k in ('site_id','logistic_type','listing_type_id')} for site in s.get('site_pricing') or []]}
                    | {'procurement':s.get('procurement')}
                    for s in ev.get('sku_details') or []],
            'market_reference':ev.get('market_reference'),'weight':ev.get('packed_weight'),'dimensions':ev.get('packed_dimensions'),
            'domestic_shipping_cny':(draft.get('pricing_plan') or {}).get('domestic_shipping_cny')}

def install_edit_resume(legacy):
    if getattr(legacy,'_intake_edit_resume_installed',False):return
    legacy._intake_edit_resume_installed=True
    original=legacy.save_edited_draft
    def save(name,changes,drafts_dir=legacy.DRAFTS_DIR,versions_dir=legacy.VERSIONS_DIR,**kwargs):
        before=editable_inputs(legacy.read_json(Path(drafts_dir)/name))
        result=original(name,changes,drafts_dir,versions_dir,**kwargs)
        destination=result[0];root=Path(drafts_dir).parent
        after=editable_inputs(legacy.read_json(destination))
        entry=root/'data/v3/auto-processing'/(Path(name).stem+'.json')
        if not kwargs.get('zying_capture') and not kwargs.get('offer_capture') and before!=after and entry.is_file():
            job=root/'jobs'/(Path(name).stem+'-full-ai-job.json')
            current=legacy.read_json(job) if job.exists() else {}
            # Explicit user pauses are retained; data-change pauses can continue.
            if current.get('status')!='paused' or current.get('pause_reason')=='DATA_CHANGED':
                enqueue(root,destination,{'updated_inputs':after})
        return result
    legacy.save_edited_draft=save
