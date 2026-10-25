"""Explicit listing-brand policy; immutable supplier evidence stays separate."""
import copy
import json
import re
from pathlib import Path

VERSION = 'LISTING_BRAND_STRICT_GENERIC_20261019_V1'


def policy(root):
    path = Path(root) / 'config/attribute_policy.json'
    value = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    return value if value.get('origin') == 'USER_BRAND_POLICY' and value.get('brand_mode') == 'STRICT_GENERIC' else {}


def apply(draft, root):
    setting = policy(root)
    if not setting:
        return False
    before = copy.deepcopy(draft)
    evidence = draft.setdefault('evidence', {})
    marker = {'origin': 'USER_BRAND_POLICY', 'value_name': 'Generic', 'enforcement': 'STRICT',
              'version': setting['version'], 'source_facts_modified': False}
    evidence['listing_brand_policy'] = marker
    from .supplier_facts_v3 import normalize_attributes
    supplier_brands = [str(normalize_attributes((evidence.get('supplier_page') or {}).get('attributes') or {}).get('品牌') or '')]

    def content(value):
        old = [str(a.get('value_name') or '') for a in value.get('attributes') or [] if a.get('id') == 'BRAND']
        value['attributes'] = [a for a in value.get('attributes') or [] if a.get('id') != 'BRAND']
        value['attributes'].append({'id': 'BRAND', 'value_id': None, 'value_name': 'Generic'})
        # Only replace direct brand claims; preserve compatibility references.
        brands = [b for b in old + supplier_brands if b and b.casefold() not in {'generic', 'general', 'other', '其他', '无品牌'}]
        for key in ['title', 'family_name']:
            text = value.get(key)
            if isinstance(text, str):
                for brand in brands:
                    text = re.sub(r'^' + re.escape(brand) + r'(?=\s)', 'Generic', text, flags=re.I)
                value[key] = text
        for site in value.get('sites_to_sell') or []:
            for brand in brands:
                if isinstance(site.get('title'), str):
                    site['title'] = re.sub(r'^' + re.escape(brand) + r'(?=\s)', 'Generic', site['title'], flags=re.I)
        completion = value.get('attribute_completion') or {}
        if completion.get('unresolved'):
            completion['unresolved'] = [k for k in completion['unresolved'] if k != 'BRAND']
        if 'attribute_completion' in value or 'attribute_origins' in value:
            value.setdefault('attribute_origins', {})['BRAND'] = 'USER_BRAND_POLICY'

    content(draft.setdefault('payload', {}))
    for sku in (evidence.get('sku_details') or []) + (evidence.get('unselected_sku_details') or []):
        sku.setdefault('user_attribute_policy', {})['BRAND'] = copy.deepcopy(marker)
        if isinstance(sku.get('ai_content'), dict):
            content(sku['ai_content'])
    evidence.setdefault('attribute_origins', {})['BRAND'] = 'USER_BRAND_POLICY'
    if (draft.get('edit_metadata') or {}).get('attribute_sources', {}).get('BRAND'):
        draft['edit_metadata']['attribute_sources']['BRAND'] = 'user_brand_policy'
    changed = draft != before
    if changed:
        draft['human_approved'] = False
    return changed


def issues(draft):
    marker = (draft.get('evidence') or {}).get('listing_brand_policy') or {}
    if marker.get('enforcement') != 'STRICT':
        return []
    evidence = draft.get('evidence') or {}
    contents = [('商品', draft.get('payload') or {})]
    contents += [('SKU ' + str(s.get('sku_id')), s['ai_content'])
                 for s in evidence.get('sku_details') or [] if isinstance(s.get('ai_content'), dict)]
    return [label + '：刊登品牌必须为Generic，请按统一品牌规则修正'
            for label, value in contents
            if len([a for a in value.get('attributes') or [] if a.get('id') == 'BRAND']) != 1
            or next((a.get('value_name') for a in value.get('attributes') or [] if a.get('id') == 'BRAND'), None) != 'Generic']
