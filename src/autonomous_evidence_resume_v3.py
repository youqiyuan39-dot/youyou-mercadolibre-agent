"""Resume only explicitly collected, materially changed evidence; never retry publication."""
import copy
import hashlib
import json
from pathlib import Path
from . import autonomous_flow_v3 as flow


def read(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid evidence record')
    return value


def source_version(root, name):
    flow_name = str(name)
    # Reuse the coordinator's filename restrictions without writing a task.
    import re
    if not re.fullmatch(r'[^/\\:]+-draft\.json', flow_name):
        raise ValueError('Invalid draft')
    root = Path(root).resolve()
    draft = read(root / 'drafts' / name)
    ev = draft.get('evidence') or {}
    relative = ev.get('source_capture') or ev.get('source_file')
    source = (root / str(relative or '')).resolve()
    allowed = ('data/intake', 'data/v3/zying_detail_payloads', 'data/v3/supplier1688')
    if not relative or not any(source.is_relative_to(root / p) for p in allowed) or not source.is_file():
        raise ValueError('Immutable source capture required')
    raw = source.read_bytes()
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Invalid source capture')
    from .auto_processing_v3 import capture_facts
    facts_hash = hashlib.sha256(json.dumps(capture_facts(data), sort_keys=True,
                                             ensure_ascii=False).encode()).hexdigest()
    return facts_hash, {'path': relative, 'sha256': hashlib.sha256(raw).hexdigest()}


def publication_seen(root, name, tasks):
    for task in tasks:
        if (task.get('stage') in {'publish', 'reconcile'}
                or (task.get('attempts') or {}).get('publish', 0)
                or 'publish' in (task.get('artifacts') or {})
                or any(e.get('stage') == 'publish' for e in task.get('events') or [])):
            return True
    filename = Path(name).stem + '.json'
    # Any record, including a failed creation, requires reconciliation first.
    return (root / 'data/publish_records' / filename).exists() or any(
        (root / 'data/v3/publishing/records').glob('*/' + filename))


def capture(root, name, source_hash, target, authorization):
    root = Path(root)
    with flow.LOCK:
        tasks = [read(p) for p in sorted((root / 'data/v3/autonomous-flow').glob('*.json'))]
        tasks = [t for t in tasks if t.get('draft') == name]
        if publication_seen(root, name, tasks):
            if not tasks:
                raise ValueError('Existing publication requires reconciliation')
            return {**copy.deepcopy(tasks[-1]), 'resume_status': 'RECONCILE_ONLY'}
        if not tasks:
            task = flow.enqueue(root, name, source_hash, target, authorization)
            try:
                actual_hash, snapshot = source_version(root, name)
                if actual_hash == source_hash:
                    task.update(source_version_scheme='capture_facts_v1', intake_source_snapshot=snapshot)
                    flow.write(root / 'data/v3/autonomous-flow' / (task['id'] + '.json'), task)
            except (ValueError, OSError):
                pass  # Legacy intake still reaches the normal evidence gate.
            return task
        active = [t for t in tasks if t.get('status') not in flow.TERMINAL]
        if active:
            return {**copy.deepcopy(active[-1]), 'resume_status': 'ACTIVE_TASK_OWNS_DRAFT'}
        parents = [t for t in tasks if t.get('status') == 'quarantined']
        if not parents:
            return {**copy.deepcopy(tasks[-1]), 'resume_status': 'NO_RECOVERABLE_TASK'}
        parent = max(parents, key=lambda t: t.get('updated_at', 0))
        if parent.get('target') != target or parent.get('authorization') != authorization:
            return {**copy.deepcopy(parent), 'resume_status': 'RECOVERY_SCOPE_CHANGED'}
        actual_hash, snapshot = source_version(root, name)
        if actual_hash != source_hash:
            raise ValueError('Capture facts hash mismatch')
        previous_hash = parent.get('source_hash')
        if parent.get('source_version_scheme') != 'capture_facts_v1':
            # A 64-character hash does not prove which historical projection
            # produced it. Observe once, then require a subsequent real change.
            baseline_path = root / 'data/v3/autonomous-evidence-baselines' / (parent['id'] + '.json')
            if not baseline_path.exists():
                flow.write(baseline_path, {'parent_id': parent['id'], 'facts_hash': actual_hash,
                                          'snapshot': snapshot, 'target': target,
                                          'authorization_fingerprint': flow.fingerprint(authorization)})
                return {**copy.deepcopy(parent), 'resume_status': 'LEGACY_BASELINE_RECORDED'}
            baseline = read(baseline_path)
            if (baseline.get('parent_id') != parent['id'] or baseline.get('target') != target
                    or baseline.get('authorization_fingerprint') != flow.fingerprint(authorization)):
                return {**copy.deepcopy(parent), 'resume_status': 'RECOVERY_SCOPE_CHANGED'}
            previous_hash = baseline.get('facts_hash')
        if actual_hash == previous_hash:
            return {**copy.deepcopy(parent), 'resume_status': 'EVIDENCE_UNCHANGED'}
        child = flow.enqueue(root, name, actual_hash, target, authorization)
        if child.get('parent_id') is None and child.get('status') == 'pending':
            child.update(parent_id=parent['id'], recovery_source_snapshot=snapshot,
                         source_version_scheme='capture_facts_v1',
                         recovery_reason='NEW_CAPTURE_FACTS')
            flow.write(root / 'data/v3/autonomous-flow' / (child['id'] + '.json'), child)
        return child


def recover_changed(root, policy):
    """Recheck only already-owned quarantined drafts; never discover old drafts."""
    root = Path(root)
    results = []
    with flow.LOCK:
        try:
            tasks = [read(p) for p in sorted((root / 'data/v3/autonomous-flow').glob('*.json'))]
        except (ValueError, OSError):
            return [{'resume_status': 'TASK_RECORD_INVALID'}]
        names = {t['draft'] for t in tasks if t.get('status') == 'quarantined'}
        for name in sorted(names):
            try:
                signature, _ = source_version(root, name)
                results.append(capture(root, name, signature, policy['target'], policy['authorization']))
            except (ValueError, OSError, KeyError, TypeError):
                # No evidence is manufactured and the previous task is retained.
                results.append({'draft': name, 'resume_status': 'SOURCE_UNAVAILABLE'})
    return results
