"""Connect durable coordination to the existing workbench business operations."""
import json
from pathlib import Path
from . import target_publish_v3 as publishing


class WorkbenchAdapter:
    def __init__(self, root, legacy):
        self.root, self.legacy = Path(root), legacy

    def __call__(self, stage, task):
        root, name = self.root, task['draft']
        path = root / 'drafts' / name
        if not path.is_file():
            return {'outcome': 'blocked', 'reason': 'SOURCE_DRAFT_MISSING'}
        draft = json.loads(path.read_text(encoding='utf-8'))
        evidence = draft.get('evidence') or {}
        source_marker = (task.get('artifacts', {}).get('evidence') or {}).get('source_snapshot')
        if stage not in {'evidence', 'reconcile'} and not source_marker:
            return {'outcome': 'blocked', 'reason': 'SOURCE_CAPTURE_BINDING_REQUIRED'}
        if stage != 'reconcile' and (stage == 'evidence' or source_marker):
            relative = str(evidence.get('source_capture') or evidence.get('source_file') or '')
            source = (root / relative).resolve()
            if not relative or not source.is_relative_to(root.resolve()) or not source.is_file():
                return {'outcome': 'blocked', 'reason': 'IMMUTABLE_SOURCE_CAPTURE_REQUIRED'}
            snapshot = {'path': relative, 'sha256': publishing.digest(source)}
            if source_marker and snapshot != source_marker:
                return {'outcome': 'blocked', 'reason': 'SOURCE_CAPTURE_CHANGED'}
        if stage == 'evidence':
            if not evidence.get('sku_details') or not evidence.get('supplier_page'):
                return {'outcome': 'blocked', 'reason': 'SUPPLIER_CAPTURE_REQUIRED'}
            return {'outcome': 'passed', 'artifact': {'draft_hash': publishing.digest(path),
                                                     'source_snapshot': snapshot}}
        if stage == 'process':
            job_path = root / 'jobs' / (path.stem + '-full-ai-job.json')
            job = json.loads(job_path.read_text(encoding='utf-8')) if job_path.exists() else {}
            marker = task['artifacts'].get('process') or {}
            if job.get('status') in {'queued', 'running'}:
                return {'outcome': 'wait', 'artifact': {'started': True, 'job': job_path.name}}
            if marker.get('started'):
                if job.get('completion_state') == 'READY_FOR_FINAL_REVIEW' and job.get('status') == 'done':
                    return {'outcome': 'passed', 'artifact': {'job': job_path.name, 'completion': job['completion_state']}}
                return {'outcome': 'blocked', 'reason': job.get('completion_state') or job.get('status') or 'JOB_MISSING',
                        'artifact': {'input_requests': job.get('input_requests') or [], 'job': job_path.name}}
            self.legacy.start_full_ai_flow(name, root / 'drafts', root)
            return {'outcome': 'wait', 'artifact': {'started': True, 'job': job_path.name}}
        if stage == 'validate':
            issues = publishing._local_issues(root, name)
            if issues:
                return {'outcome': 'blocked', 'reason': 'PRODUCT_VALIDATION', 'artifact': {'issues': issues}}
            from .integrated_pricing_v3 import summary
            pricing = summary(draft, root)
            rows = pricing.get('rows') or []
            # A stale summary retains old rows/review flags. Never treat those
            # flags as a new approval; only a complete, current estimate qualifies.
            # This guard does not manufacture an automatic budget approval.
            if (pricing.get('state') != 'BUDGET_ESTIMATE'
                    or pricing.get('errors') or pricing.get('reference_issues')
                    or pricing.get('requires_budget_review') is not False
                    or not isinstance(rows, list) or not rows
                    or not all(isinstance(r, dict) and r.get('floor_passed') is True for r in rows)):
                return {'outcome': 'blocked', 'reason': 'PROFIT_EVIDENCE_REQUIRED', 'artifact': {'state': pricing.get('state')}}
            # Use the publication service's SKU selection, including its legacy
            # single-SKU fallback. A subset of profitable rows is not coverage.
            def sku_id(row):
                value = row.get('sku_id')
                if isinstance(value, bool) or not isinstance(value, (str, int)) or not value:
                    return None
                return str(value) if str(value).strip() else None
            expected_ids = [sku_id(s) for s in publishing.service._sku_rows(draft)]
            priced_ids = [sku_id(r) for r in rows]
            if (not expected_ids or None in expected_ids or None in priced_ids
                    or len(set(expected_ids)) != len(expected_ids)
                    or len(set(priced_ids)) != len(priced_ids)
                    or set(expected_ids) != set(priced_ids)):
                return {'outcome': 'blocked', 'reason': 'PRICING_SKU_COVERAGE_MISMATCH'}
            if pricing.get('store_id') != task['target']['store_id'] or pricing.get('site_id') != task['target']['site_id']:
                return {'outcome': 'blocked', 'reason': 'PRICING_TARGET_MISMATCH'}
            return {'outcome': 'passed', 'artifact': {'draft_hash': publishing.digest(path)}}
        if stage == 'prepare':
            plan = publishing.prepare(root, name, task['target']['store_id'])['preview']
            sites = {s['site_id'] for request in plan['request'] for s in request['sites_to_sell']}
            if sites != {task['target']['site_id']}:
                return {'outcome': 'blocked', 'reason': 'PUBLICATION_TARGET_MISMATCH'}
            return {'outcome': 'passed' if plan['valid'] else 'blocked', 'reason': 'PUBLISH_PLAN',
                    'artifact': {'plan_id': plan['id'], 'issues': plan['issues']}}
        if stage == 'publish':
            if task['authorization'].get('publish') is not True:
                return {'outcome': 'blocked', 'reason': 'PUBLICATION_NOT_AUTHORIZED'}
            result = publishing.commit(root, task['artifacts']['prepare']['plan_id'], True)
            return {'outcome': 'passed', 'artifact': {'record_status': result['record']['status']}}
        if stage == 'reconcile':
            result = publishing.refresh(root, name, task['target']['store_id'])
            record = result.get('record') or {}
            sites = [s for v in record.get('variants') or [] for s in v.get('sites') or []]
            if record.get('status') == 'success' and sites and all(
                s.get('site_id') == task['target']['site_id'] and s.get('item_id')
                and s.get('marketplace_status') == 'active' and s.get('checked_at')
                and not s.get('check_error') for s in sites
            ):
                return {'outcome': 'passed', 'artifact': {'item_id': sites[0]['item_id'],
                        'item_ids': [s['item_id'] for s in sites], 'verified_live': True,
                        'sites': sites}}
            if record.get('status') in {'failed', 'partial'}:
                return {'outcome': 'blocked', 'reason': 'PLATFORM_FAILURE',
                        'artifact': {'record_status': record.get('status'), 'sites': sites}}
            return {'outcome': 'wait', 'retry_after': 60,
                    'artifact': {'record_status': record.get('status'), 'variants': record.get('variants') or []}}
        raise ValueError('Unknown stage')
