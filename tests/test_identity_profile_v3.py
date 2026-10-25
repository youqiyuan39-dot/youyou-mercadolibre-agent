import copy
import unittest

from src.identity_profile_v3 import IdentityProfile, assess, audit


class IdentityProfileAuditTests(unittest.TestCase):
    def setUp(self):
        # Synthetic audit fixture, NOT an approved production wrench profile.
        self.profile = IdentityProfile('test-only', 1, 'CBT30831', 'MLM9171',
            ('Tools', 'Hand Tools', 'Fixing', 'Wrenches'),
            ('Herramientas', 'Herramientas Manuales', 'Fijación', 'Llaves'),
            (('opening_max', 'mm'), ('units_per_pack', 'count'), ('components', 'text')))
        self.draft = {'payload': {'category_id': 'CBT30831'},
                      'evidence': {'task_evidence': {'source_sha256': 'a' * 64}}}
        self.sku = {'sku_id': 'S1'}
        self.ref = {'source_sku_id': 'S1', 'market_item_id': 'MLM123456',
            'source_capture_sha256': 'a' * 64, 'market_capture_sha256': 'b' * 64,
            'identity_category_binding': {'source_category_id': 'CBT30831',
                'market_category_id': 'MLM9171', 'source_path': list(self.profile.source_path),
                'market_path': list(self.profile.market_path)}, 'identity_evidence': []}
        for field, unit, value in [('opening_max', 'mm', 28), ('units_per_pack', 'count', 1), ('components', 'text', 'one wrench')]:
            row = {'field': field, 'supplier_value': value, 'market_value': value,
                   'supplier_unit': unit, 'market_unit': unit}
            for side, checksum, subject in [('supplier', 'a' * 64, 'S1'), ('market', 'b' * 64, 'MLM123456')]:
                row[side + '_source'] = {'archive': 'fixtures/' + side + '.json',
                    'pointer': '/attributes/' + field, 'quote': str(value),
                    'sha256': checksum, 'subject_id': subject}
            self.ref['identity_evidence'].append(row)

    def report(self):
        return audit(self.profile, self.draft, self.sku, self.ref)

    def codes(self):
        return {r['code'] for r in self.report()['issues']}

    def test_unconfigured_profile_cannot_be_created_by_reference(self):
        self.ref.update(identity_profile_id='test-only', required_identity_fields=['opening_max'])
        self.assertFalse(assess(self.draft, self.sku, self.ref)['confirmed'])
        self.assertEqual(assess(self.draft, self.sku, self.ref)['issues'][0]['code'], 'IDENTITY_PROFILE_NOT_CONFIGURED')

    def test_even_structurally_complete_proof_is_not_verified_archive(self):
        original = copy.deepcopy((self.draft, self.sku, self.ref))
        report = self.report()
        self.assertTrue(report['structurally_complete'])
        self.assertFalse(report['confirmed'])
        self.assertFalse(report['archive_values_verified'])
        self.assertEqual((self.draft, self.sku, self.ref), original)

    def test_pack_conflict_rejected(self):
        self.ref['identity_evidence'][1]['market_value'] = 2
        self.assertEqual(self.report()['state'], 'CONFLICT')
        self.assertIn('IDENTITY_VALUE_CONFLICT', self.codes())

    def test_reference_cannot_shrink_required_fields(self):
        self.ref['required_identity_fields'] = ['opening_max']
        self.ref['identity_evidence'].pop()
        self.assertIn('IDENTITY_FIELD_MISSING_OR_DUPLICATE', self.codes())

    def test_another_sku_locator_rejected(self):
        self.ref['identity_evidence'][0]['supplier_source']['subject_id'] = 'S2'
        self.assertIn('IDENTITY_PROOF_SCOPE_MISMATCH', self.codes())

    def test_wrong_category_path_rejected_even_with_same_leaf(self):
        self.ref['identity_category_binding']['market_path'][0] = 'Accesorios para Vehículos'
        self.assertIn('IDENTITY_CATEGORY_SCOPE_MISMATCH', self.codes())

    def test_changed_capture_invalidates_old_proof(self):
        self.draft['evidence']['task_evidence']['source_sha256'] = 'c' * 64
        self.assertIn('IDENTITY_SOURCE_CAPTURE_MISMATCH', self.codes())
        self.assertIn('IDENTITY_PROOF_SCOPE_MISMATCH', self.codes())

    def test_missing_source_or_wrong_unit_cannot_pass(self):
        self.ref['identity_evidence'][0]['market_source'] = 'page says 28'
        self.ref['identity_evidence'][0]['market_unit'] = 'cm'
        self.assertIn('IDENTITY_SOURCE_LOCATOR_REQUIRED', self.codes())
        self.assertIn('IDENTITY_UNIT_MISMATCH', self.codes())


if __name__ == '__main__':
    unittest.main()
