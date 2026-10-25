"""Durable end-to-end coordination; adapters supply verified business operations.

No side effect is inferred from a model answer. Publication is journaled before
dispatch and an interrupted dispatch always goes through reconciliation.
"""
import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from .atomic_json_v3 import write

LOCK = threading.RLock()
STAGES = ('evidence', 'process', 'validate', 'prepare', 'publish', 'reconcile')
TERMINAL = {'complete', 'quarantined'}
STAGE_TIMEOUTS = {'evidence': 1800, 'process': 7200, 'validate': 1800,
                  'prepare': 1800, 'publish': 900, 'reconcile': 86400}
MAX_EVENTS = 200


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def enqueue(root, draft, source_hash, target, authorization):
    if not re.fullmatch(r'[^/\\:]+-draft\.json', str(draft)):
        raise ValueError('Invalid draft')
    if not source_hash or not target.get('store_id') or not target.get('site_id'):
        raise ValueError('Source and explicit target required')
    if not authorization.get('reference'):
        raise ValueError('Authorization provenance required')
    identity = fingerprint([draft, source_hash, target])
    path = Path(root) / 'data/v3/autonomous-flow' / (identity + '.json')
    with LOCK:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        task = dict(id=identity, draft=draft, source_hash=source_hash,
                    target=copy.deepcopy(target), authorization=copy.deepcopy(authorization),
                    status='pending', stage='evidence', attempts={}, artifacts={}, events=[], next_at=0)
        write(path, task)
        return task


def advance(root, task_id, adapter, now=None, max_attempts=3):
    if not re.fullmatch(r'[a-f0-9]{64}', str(task_id)):
        raise ValueError('Invalid task id')
    now = time.time() if now is None else now
    path = Path(root) / 'data/v3/autonomous-flow' / (task_id + '.json')
    # Serial coordinator: ownership spans external dispatch, preventing two workers
    # in this backend from creating the same listing concurrently.
    with LOCK:
        task = json.loads(path.read_text(encoding='utf-8'))
        if task['status'] in TERMINAL or task.get('next_at', 0) > now:
            return task
        if task['status'] == 'dispatching':
            task['stage'] = 'reconcile'
            task['status'] = 'pending'
        stage = task['stage']
        if (task['status'] == 'running' and stage == 'process'
                and not (task.get('artifacts', {}).get('process') or {}).get('started')):
            # We cannot tell whether the previous start call was accepted. Do
            # not re-run an old completed job or start a second processing job.
            task.update(status='quarantined', reason='PROCESS_DISPATCH_UNKNOWN')
            write(path, task)
            return task
        entered = task.setdefault('stage_started_at', {}).setdefault(stage, now)
        if now - entered >= STAGE_TIMEOUTS[stage]:
            task.update(status='quarantined', reason=('PUBLICATION_OUTCOME_UNRESOLVED'
                        if stage == 'reconcile' else 'STAGE_TIMEOUT:' + stage), updated_at=now)
            write(path, task)
            return task
        attempts = task['attempts'].get(stage, 0)
        if attempts >= max_attempts:
            task.update(status='quarantined', reason='RETRY_LIMIT:' + stage)
            write(path, task)
            return task
        task['attempts'][stage] = attempts + 1
        task.update(status='dispatching' if stage == 'publish' else 'running', updated_at=now)
        write(path, task)
        try:
            result = adapter(stage, copy.deepcopy(task))
            if not isinstance(result, dict) or result.get('outcome') not in {'passed', 'retry', 'blocked', 'unknown', 'wait'}:
                raise ValueError('Adapter must return an explicit outcome')
        except Exception as exc:
            # A failed publish transport may have created a remote item. Never
            # retry creation from an exception alone.
            result = {'outcome': 'unknown' if stage == 'publish' else 'retry',
                      'reason': type(exc).__name__}
        outcome = result['outcome']
        task['events'].append({'stage': stage, 'outcome': outcome, 'at': now,
                               'reason': result.get('reason', '')})
        overflow = max(0, len(task['events']) - MAX_EVENTS)
        task['events_pruned'] = task.get('events_pruned', 0) + overflow
        task['events'] = task['events'][-MAX_EVENTS:]
        if result.get('artifact') is not None:
            task['artifacts'][stage] = result['artifact']
        if stage == 'publish' and outcome != 'blocked':
            task.update(stage='reconcile', status='pending', next_at=now + 20)
        elif outcome == 'wait':
            task['attempts'][stage] = attempts
            task.update(status='pending', next_at=now + max(1, min(300, result.get('retry_after', 10))))
        elif outcome == 'blocked':
            task.update(status='quarantined', reason=result.get('reason', 'EVIDENCE_MISSING'))
        elif outcome in {'retry', 'unknown'}:
            task.update(status='pending', next_at=now + min(300, 20 * 2**attempts))
        elif stage == 'reconcile':
            receipt = result.get('artifact') or {}
            if not receipt.get('item_id') or receipt.get('verified_live') is not True:
                task.update(status='quarantined', reason='LIVE_RECEIPT_MISSING')
            else:
                task.update(status='complete', finished_at=now)
        else:
            task.update(stage=STAGES[STAGES.index(stage) + 1], status='pending', next_at=now)
        write(path, task)
        return task


def sweep(root, adapter, now=None):
    results = []
    for path in sorted((Path(root) / 'data/v3/autonomous-flow').glob('*.json')):
        try:
            results.append(advance(root, path.stem, adapter, now))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            results.append({'id': path.stem, 'status': 'record_error', 'reason': type(exc).__name__})
    return results
