"""Read-only, offline inventory of recorded automation evidence, not a release gate.

Only drafts, immutable captures, existing job/task records and selected local image
files are read. No account/configuration, model, network or task runner is loaded.
Run: python -m src.automation_readiness_v3 --root PROJECT [--output WORKSPACE/file.json]
"""
import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re

VERSION = 'AUTOMATION_READINESS_20261023_V1'
WORKSPACE = Path.home() / 'Desktop/workspace'
MAX_JSON_BYTES = 10_000_000


def _read(path):
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('JSON_TOO_LARGE')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('OBJECT_REQUIRED')
    return value


def _code(value):
    # No free-form errors, descriptions, URLs, authorizations or page payloads.
    text = str(value or '')
    return text[:100] if re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', text) else 'UNKNOWN'


def _positive(value):
    try:
        n = Decimal(str(value)) if not isinstance(value, bool) else Decimal('NaN')
        return n.is_finite() and n > 0
    except (ValueError, InvalidOperation):
        return False


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _past_epoch(value, now):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= now


def _receipt_matches(receipt, task, now):
    sites = receipt.get('sites')
    item_ids = receipt.get('item_ids')
    target = (task.get('target') or {}).get('site_id')
    if (receipt.get('verified_live') is not True or not target or not isinstance(sites, list)
            or not sites or not isinstance(item_ids, list) or not item_ids):
        return False
    seen = []
    for site in sites:
        if (not isinstance(site, dict) or site.get('site_id') != target
                or site.get('marketplace_status') != 'active' or not site.get('item_id')
                or site.get('check_error')):
            return False
        try:
            checked = datetime.fromisoformat(str(site.get('checked_at') or '').replace('Z', '+00:00'))
            if checked.tzinfo is None or not _past_epoch(checked.timestamp(), now):
                return False
        except (ValueError, TypeError, OSError):
            return False
        seen.append(site['item_id'])
    return (all(isinstance(i, str) and i for i in item_ids + seen)
            and len(set(seen)) == len(seen) and len(set(item_ids)) == len(item_ids)
            and set(seen) == set(item_ids) and receipt.get('item_id') in set(item_ids))


def _capture_hash(value):
    # Same fact projection as auto_processing_v3.capture_facts; no queue import.
    value = copy.deepcopy({k: v for k, v in value.items() if k not in {'captured_at', 'collected_at'}})
    if isinstance(value.get('api_evidence'), dict):
        value['api_evidence'].pop('observed_at', None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _check(stage, status, code, action, count=0, tool='UNKNOWN'):
    return {'stage': stage, 'status': status, 'code': code, 'affected_sku_count': count,
            'next_action': action, 'tool_availability': tool}


def _source(root, draft):
    relative = (draft.get('evidence') or {}).get('source_capture') or (draft.get('evidence') or {}).get('source_file')
    source = (root / str(relative or '')).resolve()
    allowed = ('data/intake', 'data/v3/zying_detail_payloads', 'data/v3/supplier1688')
    if not relative or not any(source.is_relative_to(root / folder) for folder in allowed) or not source.is_file():
        return _check('source', 'BLOCKED', 'IMMUTABLE_SOURCE_CAPTURE_REQUIRED', 'RECOVER_CAPTURE', tool='EXTERNAL_OR_RECOLLECTION_REQUIRED'), None
    try:
        capture = _read(source)
        return _check('source', 'RECORDED', 'CAPTURE_READABLE', 'KEEP_CAPTURE'), {
            'path': str(relative), 'sha256': _hash(source), 'facts_sha256': _capture_hash(capture)}
    except (ValueError, OSError):
        return _check('source', 'BLOCKED', 'SOURCE_CAPTURE_UNREADABLE', 'RECOVER_CAPTURE'), None


def _images(root, ev, skus):
    review = ev.get('image_review') or {}
    groups = review.get('sku_listing_galleries') or []
    counts, unavailable, unreviewed = [], 0, 0
    for sku in skus:
        matches = [g for g in groups if str(g.get('sku_id')) == str(sku.get('sku_id'))]
        gallery = matches[0].get('gallery') or [] if len(matches) == 1 else []
        if not groups and len(skus) == 1:
            gallery = review.get('gallery') or []
        unique = {}
        for image in gallery:
            if not isinstance(image, dict) or image.get('review_status') == 'REJECTED':
                continue
            key = image.get('screen_fingerprint') or image.get('local_file') or image.get('remote_url')
            if key:
                unique.setdefault(str(key), image)
        counts.append(len(unique))
        for image in unique.values():
            if image.get('review_status') not in {'AUTO_SCREENED', 'HUMAN_SELECTED', 'APPROVED'} and ev.get('images_human_reviewed') is not True:
                unreviewed += 1
            path = (root / 'assets' / str(image.get('local_file') or '')).resolve()
            if not image.get('local_file') or not path.is_relative_to(root / 'assets') or not path.is_file():
                unavailable += 1
                continue
            try:
                from PIL import Image
                with Image.open(path) as value:
                    value.verify()
            except (ImportError, OSError, ValueError):
                unavailable += 1
    short = sum(count < 3 for count in counts)
    if not skus or short:
        return _check('images', 'BLOCKED', 'IMAGE_COUNT_INSUFFICIENT', 'RECOVER_EXACT_SKU_ORIGINALS', short or len(skus), 'SOURCE_IMAGE_RECOVERY_IF_CAPTURE_HAS_BINDING')
    if unavailable or unreviewed:
        return _check('images', 'UNKNOWN', 'IMAGE_BYTES_OR_QUALITY_UNVERIFIED', 'VERIFY_BOUND_IMAGE_BYTES_AND_QUALITY', len(skus))
    return _check('images', 'RECORDED', 'THREE_LOCAL_SELECTED_IMAGES_PER_SKU', 'KEEP_VALID_IMAGES')


def audit_draft(root, name, draft, tasks=(), job=None, since=None):
    """Whitelist summary only; RECORDED never means independently verified facts."""
    root = Path(root).resolve()
    ev, payload = draft.get('evidence') or {}, draft.get('payload') or {}
    skus = [s for s in ev.get('sku_details') or [] if isinstance(s, dict)]
    checks = []
    source_check, source = _source(root, draft)
    checks.append(source_check)
    sku_ids = [str(s.get('sku_id') or '').strip() for s in skus]
    sku_identity_ok = bool(sku_ids) and all(sku_ids) and len(set(sku_ids)) == len(sku_ids)
    checks.append(_check('sku_identity', 'RECORDED' if sku_identity_ok else 'BLOCKED',
                         'EXPLICIT_SKU_IDS_RECORDED' if sku_identity_ok else 'SKU_IDENTITY_REQUIRED',
                         'KEEP_SKU_IDENTITIES' if sku_identity_ok else 'RECOVER_EXPLICIT_PURCHASE_SKU',
                         0 if sku_identity_ok else len(skus), 'EXISTING_SOURCE_RECOVERY_IF_CAPTURE_HAS_SKU_IDS'))
    matching = ev.get('product_matching_review') or {}
    rows = matching.get('rows') or []
    linked = len(skus) > 0 and len(rows) == len(skus) and {str(r.get('sku_id')) for r in rows} == {str(s.get('sku_id')) for s in skus}
    if any(r.get('state') == 'CONFLICT' for r in rows):
        checks.append(_check('matching', 'BLOCKED', 'MATCH_CONFLICT_RECORDED', 'RECOVER_IDENTITY_OR_SELECT_OTHER_OFFER', tool='NEW_IDENTITY_EVIDENCE_REQUIRED'))
    elif linked and all(r.get('state') == 'CONFIRMED' and not r.get('issues') for r in rows):
        checks.append(_check('matching', 'RECORDED', 'CONFIRMED_MAPPING_RECORDED', 'RECHECK_MAPPING_SOURCE_CURRENCY'))
    else:
        checks.append(_check('matching', 'UNKNOWN', 'EXACT_SKU_MATCH_UNVERIFIED', 'RECOVER_EXACT_SKU_IDENTITY_EVIDENCE', len(skus), 'NEW_IDENTITY_EVIDENCE_REQUIRED'))
    missing_pack = 0
    for sku in skus:
        package = sku.get('package') or {}
        states = [str(package.get('status') or '')] + list((package.get('field_status') or {}).values())
        if not all(_positive(package.get(k)) for k in ('weight_g', 'length_cm', 'width_cm', 'height_cm')) or any(any(marker in str(state).upper() for marker in ('UNVERIFIED', 'UNCONFIRMED', 'ESTIMATE', 'DEFAULT')) for state in states):
            missing_pack += 1
    checks.append(_check('packaging', 'BLOCKED' if missing_pack or not skus else 'RECORDED', 'PACKAGING_EVIDENCE_REQUIRED' if missing_pack or not skus else 'COMPLETE_PACKAGE_VALUES_RECORDED', 'RECOVER_EXPLICIT_PACKAGING' if missing_pack or not skus else 'KEEP_PACKAGE_EVIDENCE', missing_pack, 'EXISTING_PACKAGE_RECOVERY_TOOL'))
    checks.append(_images(root, ev, skus))
    unresolved = sum(len(((s.get('ai_content') or {}).get('attribute_completion') or {}).get('unresolved') or []) for s in skus)
    category = (ev.get('classification_review') or {}).get('status')
    attrs_known = bool(skus) and all((s.get('ai_content') or {}).get('attribute_completion') is not None for s in skus)
    checks.append(_check('attributes', 'BLOCKED' if unresolved or category == 'NEEDS_REVIEW' else 'RECORDED' if attrs_known and payload.get('category_id') else 'UNKNOWN', 'REQUIRED_ATTRIBUTE_OR_CATEGORY_GAP' if unresolved or category == 'NEEDS_REVIEW' else 'ATTRIBUTE_RECORDS_PRESENT' if attrs_known else 'ATTRIBUTE_VALIDATION_UNAVAILABLE', 'REVALIDATE_CURRENT_CATEGORY_AND_ATTRIBUTES', unresolved, 'EXISTING_LOCAL_VALIDATORS'))
    content_count = sum((s.get('ai_processing') or {}).get('status') == 'done' and bool((s.get('ai_content') or {}).get('description')) for s in skus)
    checks.append(_check('content', 'RECORDED' if skus and content_count == len(skus) else 'BLOCKED', 'CONTENT_RECORDED' if skus and content_count == len(skus) else 'CONTENT_INCOMPLETE', 'RESUME_FAILED_CONTENT_STEPS', len(skus) - content_count, 'EXISTING_SKU_PROCESSING_TOOL'))
    prices = ev.get('pricing_result') or ev.get('ai_processing_pricing') or {}
    budget_review = prices.get('requires_budget_review') is True
    # Neither a local calculator's numerical pass nor saved budget rows prove
    # official acquisition, current fees or publish eligibility.
    checks.append(_check('pricing', 'BLOCKED' if budget_review or not prices else 'UNKNOWN', 'BUDGET_REVIEW_NOT_PROFIT_PASS' if budget_review else 'PROFIT_EVIDENCE_UNAVAILABLE' if not prices else 'PROFIT_PROOF_NOT_INDEPENDENTLY_VERIFIED', 'ACQUIRE_BOUND_CURRENT_COST_AND_FEE_EVIDENCE', len(skus), 'OFFICIAL_CALCULATOR_ACQUISITION_NOT_CONNECTED'))
    task_rows = []
    current_complete = 0
    now = datetime.now(timezone.utc).timestamp()
    for task in tasks:
        artifact = task.get('artifacts') or {}
        marker = (artifact.get('evidence') or {}).get('source_snapshot')
        receipt = artifact.get('reconcile') or {}
        bound = bool(source and marker == {'path': source['path'], 'sha256': source['sha256']} and task.get('source_hash') == source['facts_sha256'])
        timestamp = task.get('finished_at')
        fresh = _past_epoch(timestamp, now) and _past_epoch(since, now) and timestamp >= since
        complete = task.get('status') == 'complete' and bound and fresh and _receipt_matches(receipt, task, now)
        current_complete += bool(complete)
        status, stage = _code(task.get('status')), _code(task.get('stage'))
        task_rows.append({'id': _code(task.get('id')), 'status': status, 'stage': stage,
                          'reason_code': _code(task.get('reason')), 'source_binding_matches': bound,
                          'completion_scope': 'CURRENT_RECORDED_NOT_LIVE_RECHECKED' if complete else 'HISTORICAL_OR_UNVERIFIED' if status == 'complete' else 'NOT_COMPLETE'})
        if (stage == 'reconcile' and status != 'complete') or status == 'dispatching':
            checks.insert(0, _check('coordinator', 'BLOCKED', 'PUBLICATION_OUTCOME_REQUIRES_RECONCILIATION', 'RECONCILE_EXISTING_PUBLICATION_DO_NOT_RESUBMIT', tool='EXISTING_RECONCILIATION_TOOL'))
        elif not bound and stage != 'evidence':
            checks.insert(0, _check('coordinator', 'BLOCKED', 'SOURCE_CAPTURE_BINDING_REQUIRED', 'REBIND_IMMUTABLE_SOURCE_BEFORE_RESUME'))
    first = next((c for c in checks if c['status'] in {'BLOCKED', 'UNKNOWN'}), None)
    input_codes = sorted({_code(r.get('code') or r.get('cause')) for r in (job or {}).get('input_requests') or [] if isinstance(r, dict)})
    return {'draft': name, 'sku_count': len(skus), 'checks': checks, 'earliest_gap': first,
            'readiness': 'BLOCKED_OR_UNKNOWN' if first else 'RECORDED_ONLY_NOT_PUBLISH_AUTHORIZATION',
            'tasks': task_rows, 'current_recorded_completion_count': current_complete,
            'job': {'status': _code((job or {}).get('status')), 'completion_state': _code((job or {}).get('completion_state')), 'input_request_codes': input_codes},
            'pricing_state_recorded': _code(prices.get('state') or prices.get('status')),
            'live_checks_performed': False, 'side_effects_performed': False}


def audit(root, since=None):
    root = Path(root).resolve()
    if since is not None and not _past_epoch(since, datetime.now(timezone.utc).timestamp()):
        raise ValueError('since must be a finite, nonfuture timestamp')
    tasks, errors, rows = {}, [], []
    for path in sorted((root / 'data/v3/autonomous-flow').glob('*.json')):
        try:
            value = _read(path)
            tasks.setdefault(str(value.get('draft') or ''), []).append(value)
        except (ValueError, OSError):
            errors.append({'kind': 'TASK_RECORD_UNREADABLE', 'file': path.name})
    for path in sorted((root / 'drafts').glob('*-draft.json')):
        try:
            job_path = root / 'jobs' / (path.stem + '-full-ai-job.json')
            job = _read(job_path) if job_path.is_file() else {}
            rows.append(audit_draft(root, path.name, _read(path), tasks.get(path.name, []), job, since))
        except (ValueError, OSError, TypeError, KeyError, AttributeError):
            errors.append({'kind': 'DRAFT_OR_JOB_RECORD_INVALID', 'file': path.name})
    names = {r['draft'] for r in rows}
    orphan_count = sum(len(v) for k, v in tasks.items() if k not in names)
    return {'version': VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'LOCAL_RECORDED_EVIDENCE_ONLY', 'since_epoch': since,
            'summary': {'draft_count': len(rows), 'count_unit': 'DRAFT_FILES_NOT_DISTINCT_PRODUCTS', 'task_count': sum(map(len, tasks.values())),
                        'orphan_task_count': orphan_count, 'record_errors': len(errors),
                        'current_recorded_completion_count': sum(r['current_recorded_completion_count'] for r in rows),
                        'earliest_gap_codes': dict(Counter(r['earliest_gap']['code'] if r['earliest_gap'] else 'NONE' for r in rows))},
            'drafts': rows, 'record_errors': errors,
            'limitations': ['No account/configuration read; runtime authorization is unknown.',
                'Recorded fields and hashes do not establish supplier truth or official quote authenticity.',
                'Calculator acquisition is not connected in official_budget_evidence_v3; calculation-only is not profit pass.',
                'No live platform check; historical publication does not count as this run completion.',
                'No network, model invocation, task start, draft write or publication.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--since', help='Timezone-aware ISO timestamp; required to scope recorded completions to this run')
    parser.add_argument('--output', type=Path, help='Explicit new JSON output within workspace; never overwrites')
    args = parser.parse_args(argv)
    since = None
    if args.since:
        try:
            stamp = datetime.fromisoformat(args.since.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError()
            since = stamp.timestamp()
            if not _past_epoch(since, datetime.now(timezone.utc).timestamp()):
                raise ValueError()
        except ValueError:
            parser.error('--since requires an ISO timestamp with timezone')
    value = audit(args.root, since)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if args.output:
        path = args.output.resolve()
        if not path.is_relative_to(WORKSPACE.resolve()) or path.suffix.lower() != '.json':
            parser.error('--output must be a JSON path inside workspace')
        if path.exists():
            parser.error('--output already exists; choose a new evidence file')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as file:
            file.write(text + '\n')
        print(json.dumps(value['summary'], ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
