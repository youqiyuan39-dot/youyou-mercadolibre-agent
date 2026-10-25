"""Server-owned identity profiles. Not connected to production match approval.

No profile is approved yet: a category name does not enumerate every physical
distinction. Audits produce diagnostics, never a confirmed same-SKU decision.
Evidence values below must eventually be resolved against their archived files;
hash-shaped strings and caller-provided equal values alone are not provenance.
"""
from dataclasses import dataclass
from types import MappingProxyType
import re

VERSION = 'IDENTITY_PROFILE_20261023_V1_AUDIT_ONLY'


@dataclass(frozen=True)
class IdentityProfile:
    profile_id: str
    version: int
    source_category_id: str
    market_category_id: str
    source_path: tuple
    market_path: tuple
    # Tuple of (field name, canonical unit), declared in server code only.
    fields: tuple


# Deliberately empty until an evidence-backed type-specific profile and archive
# resolver are approved. A reference cannot supply or shrink its own profile.
PROFILES = MappingProxyType({})


def assess(draft, sku, reference):
    profile = PROFILES.get(str(reference.get('identity_profile_id') or ''))
    if profile is None:
        return {'version': VERSION, 'state': 'CANDIDATE', 'confirmed': False,
                'issues': [{'code': 'IDENTITY_PROFILE_NOT_CONFIGURED'}]}
    return audit(profile, draft, sku, reference)


def audit(profile, draft, sku, reference):
    """Pure audit for a trusted profile; never grants publishing eligibility."""
    issues = []
    def fail(code, field=None):
        issues.append({'code': code, **({'field': field} if field else {})})

    ev = draft.get('evidence') or {}
    source_hash = (ev.get('task_evidence') or {}).get('source_sha256')
    sku_id = str(sku.get('sku_id') or '')
    item_id = str(reference.get('market_item_id') or '')
    market_hash = reference.get('market_capture_sha256')
    sha = lambda value: isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))
    if not sku_id or reference.get('source_sku_id') != sku_id:
        fail('IDENTITY_SKU_SCOPE_MISMATCH')
    if not item_id or reference.get('resolved_item_id', item_id) != item_id:
        fail('IDENTITY_MARKET_ITEM_MISMATCH')
    if not sha(source_hash) or reference.get('source_capture_sha256') != source_hash:
        fail('IDENTITY_SOURCE_CAPTURE_MISMATCH')
    if not sha(market_hash):
        fail('IDENTITY_MARKET_CAPTURE_REQUIRED')
    category = reference.get('identity_category_binding') or {}
    if ((draft.get('payload') or {}).get('category_id') != profile.source_category_id
            or category.get('source_category_id') != profile.source_category_id
            or category.get('market_category_id') != profile.market_category_id
            or tuple(category.get('source_path') or []) != profile.source_path
            or tuple(category.get('market_path') or []) != profile.market_path):
        fail('IDENTITY_CATEGORY_SCOPE_MISMATCH')
    if not profile.fields or len({f for f, _ in profile.fields}) != len(profile.fields):
        fail('IDENTITY_PROFILE_INVALID')
    proof = reference.get('identity_evidence') or []
    if not isinstance(proof, list):
        proof = []
    for field, unit in profile.fields:
        rows = [p for p in proof if isinstance(p, dict) and p.get('field') == field]
        if len(rows) != 1:
            fail('IDENTITY_FIELD_MISSING_OR_DUPLICATE', field)
            continue
        row = rows[0]
        left, right = row.get('supplier_value'), row.get('market_value')
        if left is None or right is None or left == '' or right == '' or isinstance(left, bool) or isinstance(right, bool):
            fail('IDENTITY_VALUE_UNKNOWN', field)
        elif type(left) is not type(right) or left != right:
            fail('IDENTITY_VALUE_CONFLICT', field)
        if row.get('supplier_unit') != unit or row.get('market_unit') != unit:
            fail('IDENTITY_UNIT_MISMATCH', field)
        for side, expected_hash, expected_id in (('supplier', source_hash, sku_id), ('market', market_hash, item_id)):
            locator = row.get(side + '_source')
            if not isinstance(locator, dict) or not all(isinstance(locator.get(k), str) and locator[k].strip() for k in ('archive', 'pointer', 'quote')):
                fail('IDENTITY_SOURCE_LOCATOR_REQUIRED', field)
                continue
            if locator.get('sha256') != expected_hash or locator.get('subject_id') != expected_id:
                fail('IDENTITY_PROOF_SCOPE_MISMATCH', field)
    conflict = any(i['code'] in {'IDENTITY_VALUE_CONFLICT', 'IDENTITY_SKU_SCOPE_MISMATCH',
                                'IDENTITY_CATEGORY_SCOPE_MISMATCH', 'IDENTITY_PROOF_SCOPE_MISMATCH',
                                'IDENTITY_SOURCE_CAPTURE_MISMATCH', 'IDENTITY_MARKET_ITEM_MISMATCH'} for i in issues)
    return {'version': VERSION, 'profile_id': profile.profile_id, 'profile_version': profile.version,
            'state': 'CONFLICT' if conflict else 'CANDIDATE', 'confirmed': False,
            'structurally_complete': not issues, 'archive_values_verified': False,
            'next_action': 'RESOLVE_ARCHIVED_VALUES_AND_APPROVE_PROFILE' if not issues else 'REPAIR_IDENTITY_EVIDENCE',
            'issues': issues}
