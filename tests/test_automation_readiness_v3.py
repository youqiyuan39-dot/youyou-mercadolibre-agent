"""Offline audit: recorded success, budgets and history must not become release."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import automation_readiness_v3 as readiness

PROJECT = Path(__file__).resolve().parents[1]


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / 'docs/automation-methodology-20261023'
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.capture = {'title': 'Public source', 'skus': [{'sku_id': 's'}]}
        self.source = self.root / 'data/intake/source.json'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(self.capture), encoding='utf-8')
        self.draft = {'payload': {}, 'evidence': {'source_capture': 'data/intake/source.json',
            'supplier_page': {}, 'sku_details': [{'sku_id': 's'}]}}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_offline_readonly_and_whitelist(self):
        self.draft['evidence']['supplier_page']['description'] = 'PRIVATE_PAGE_TEXT_MUST_NOT_APPEAR'
        self.draft['evidence']['ai_processing_pricing'] = {'state': 'BUDGET_ESTIMATE', 'requires_budget_review': True,
            'rows': [{'floor_passed': True}], 'unknown_private_detail': 'PRIVATE_PRICE_PAYLOAD'}
        path = self.write('drafts/sample-draft.json', self.draft)
        before = path.read_bytes()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            result = readiness.audit(self.root)
        text = json.dumps(result)
        self.assertNotIn('PRIVATE_PAGE_TEXT_MUST_NOT_APPEAR', text)
        self.assertNotIn('PRIVATE_PRICE_PAYLOAD', text)
        self.assertEqual(path.read_bytes(), before)
        pricing = next(c for c in result['drafts'][0]['checks'] if c['stage'] == 'pricing')
        self.assertEqual(pricing['code'], 'BUDGET_REVIEW_NOT_PROFIT_PASS')
        self.assertEqual(result['summary']['current_recorded_completion_count'], 0)

    def test_source_cannot_point_at_configuration_or_outside_root(self):
        self.write('config/private.json', {'secret': 'NOT_READ'})
        for relative in ['config/private.json', '../outside.json']:
            draft = copy.deepcopy(self.draft)
            draft['evidence']['source_capture'] = relative
            with patch.object(readiness, '_read', side_effect=AssertionError('must not read')):
                row = readiness.audit_draft(self.root, 'sample-draft.json', draft)
            self.assertEqual(row['earliest_gap']['code'], 'IMMUTABLE_SOURCE_CAPTURE_REQUIRED')

    def test_match_is_not_inferred_from_title_or_image_similarity(self):
        self.draft['payload']['title'] = 'Exact same title'
        self.draft['evidence']['market_reference'] = {'title': 'Exact same title', 'spec_match': 'VERIFIED_SAME_SKU'}
        row = readiness.audit_draft(self.root, 'sample-draft.json', self.draft)
        self.assertEqual(row['earliest_gap']['code'], 'EXACT_SKU_MATCH_UNVERIFIED')

    def test_missing_empty_or_duplicate_sku_identity_precedes_matching(self):
        for skus in [[], [{}], [{'sku_id': 'same'}, {'sku_id': 'same'}]]:
            draft = copy.deepcopy(self.draft)
            draft['payload']['seller_sku'] = 'EDITOR_PAYLOAD_NOT_PURCHASE_SKU'
            draft['evidence']['sku_details'] = skus
            row = readiness.audit_draft(self.root, 'sample-draft.json', draft)
            self.assertEqual(row['earliest_gap']['code'], 'SKU_IDENTITY_REQUIRED')
            self.assertEqual(row['earliest_gap']['next_action'], 'RECOVER_EXPLICIT_PURCHASE_SKU')

    def test_saved_conflict_and_remote_images_remain_blocked_or_unknown(self):
        ev = self.draft['evidence']
        ev['product_matching_review'] = {'rows': [{'sku_id': 's', 'state': 'CONFLICT'}]}
        ev['image_review'] = {'sku_listing_galleries': [{'sku_id': 's', 'gallery': [
            {'remote_url': 'https://example.invalid/' + str(i), 'review_status': 'AUTO_SCREENED'} for i in range(3)]}]}
        row = readiness.audit_draft(self.root, 'sample-draft.json', self.draft)
        self.assertEqual(row['earliest_gap']['code'], 'MATCH_CONFLICT_RECORDED')
        self.assertEqual(next(c for c in row['checks'] if c['stage'] == 'images')['status'], 'UNKNOWN')

    def task(self):
        return {'id': 'a' * 64, 'status': 'complete', 'stage': 'reconcile', 'draft': 'sample-draft.json',
            'target': {'site_id': 'MLM'},
            'finished_at': 2000, 'source_hash': readiness._capture_hash(self.capture), 'artifacts': {
                'evidence': {'source_snapshot': {'path': 'data/intake/source.json', 'sha256': readiness._hash(self.source)}},
                'reconcile': {'verified_live': True, 'item_id': 'MLM123456789', 'item_ids': ['MLM123456789'],
                    'sites': [{'site_id': 'MLM', 'item_id': 'MLM123456789', 'marketplace_status': 'active',
                               'checked_at': '1970-01-01T00:33:20+00:00'}]}}}

    def test_history_never_counts_without_explicit_run_scope_and_bound_receipt(self):
        task = self.task()
        self.assertEqual(readiness.audit_draft(self.root, 'sample-draft.json', self.draft, [task])['current_recorded_completion_count'], 0)
        self.assertEqual(readiness.audit_draft(self.root, 'sample-draft.json', self.draft, [task], since=1000)['current_recorded_completion_count'], 1)
        for mutate in [lambda t: t['artifacts']['reconcile'].update(verified_live=False),
                       lambda t: t.update(source_hash='changed'), lambda t: t.update(finished_at=999),
                       lambda t: t.update(finished_at=float('nan')), lambda t: t.update(finished_at=float('inf')),
                       lambda t: t.update(finished_at=99999999999),
                       lambda t: t['artifacts']['reconcile'].update(sites=[]),
                       lambda t: t['artifacts']['reconcile'].update(item_ids=['MLM987654321']),
                       lambda t: t['artifacts']['reconcile']['sites'][0].update(site_id='MLB'),
                       lambda t: t['artifacts']['reconcile']['sites'][0].update(marketplace_status='paused'),
                       lambda t: t['artifacts']['reconcile']['sites'][0].update(checked_at='2999-01-01T00:00:00Z'),
                       lambda t: t['artifacts']['reconcile']['sites'][0].update(check_error='CHECK_FAILED')]:
            wrong = copy.deepcopy(task)
            mutate(wrong)
            self.assertEqual(readiness.audit_draft(self.root, 'sample-draft.json', self.draft, [wrong], since=1000)['current_recorded_completion_count'], 0)

    def test_invalid_or_future_since_rejected(self):
        for value in [float('nan'), float('inf'), 99999999999]:
            with self.assertRaises(ValueError):
                readiness.audit(self.root, since=value)
        with patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit):
                readiness.main(['--root', str(self.root), '--since', '2999-01-01T00:00:00Z'])

    def test_dispatch_unknown_demands_reconciliation_not_republish(self):
        task = {'status': 'dispatching', 'stage': 'publish', 'authorization': {'secret': 'MUST_NOT_OUTPUT'}}
        row = readiness.audit_draft(self.root, 'sample-draft.json', self.draft, [task])
        self.assertEqual(row['earliest_gap']['next_action'], 'RECONCILE_EXISTING_PUBLICATION_DO_NOT_RESUBMIT')
        self.assertNotIn('MUST_NOT_OUTPUT', json.dumps(row))

    def test_invalid_job_does_not_abort_other_drafts(self):
        self.write('drafts/valid-draft.json', self.draft)
        self.write('drafts/bad-draft.json', {'evidence': {'sku_details': []}})
        self.write('jobs/bad-draft-full-ai-job.json', ['invalid'])
        report = readiness.audit(self.root)
        self.assertEqual(report['summary']['draft_count'], 1)
        self.assertEqual(report['summary']['record_errors'], 1)

    def test_cli_writes_only_new_explicit_workspace_json(self):
        self.write('drafts/sample-draft.json', self.draft)
        destination = self.root / 'result.json'
        with patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(readiness.main(['--root', str(self.root), '--output', str(destination)]), 0)
        self.assertTrue(destination.is_file())
        with patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit):
                readiness.main(['--root', str(self.root), '--output', str(destination)])
            with self.assertRaises(SystemExit):
                readiness.main(['--root', str(self.root), '--output', str(readiness.WORKSPACE.parent / 'audit-outside-must-not-exist.json')])


if __name__ == '__main__':
    unittest.main()
