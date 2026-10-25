"""Only surface missing GTIN when an official category rule explicitly requires it."""
from pathlib import Path
import json
import re


def barcode_issues(draft, *, root=None, category=None):
    payload = draft.get('payload') or {}
    evidence = draft.get('evidence') or {}
    category_id = str(payload.get('category_id') or '')
    if not re.fullmatch(r'[A-Z]{3}\d+', category_id):
        return []
    if category is None:
        try:
            category = json.loads((Path(root or Path(__file__).resolve().parents[1]) / 'data/categories' / (category_id + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
    if category.get('category_id') != category_id or category.get('query_source') != 'MERCADOLIBRE_OFFICIAL_API_LIVE':
        return []
    gtin_rule = next((row for row in category.get('all_attributes') or [] if row.get('id') == 'GTIN'), {})
    tags = gtin_rule.get('tags') or []
    tags = {key for key, value in tags.items() if value} if isinstance(tags, dict) else set(tags)
    condition = str(payload.get('condition') or 'new').lower()
    if str(payload.get('item_condition_value_id') or '') in {'2230581', '2230582'}:
        condition = 'used'
    if not ('required' in tags or ('new_required' in tags and condition == 'new')):
        # Conditional rules and a pending no-code marker do not prove GTIN is mandatory.
        # Brand-specific rejections remain visible through the actual platform error.
        return []
    common_gtin = evidence.get('gtin') or payload.get('gtin') or next((row.get('value_name') for row in payload.get('attributes') or [] if row.get('id') in {'GTIN', 'EAN', 'UPC'} and row.get('value_name')), '')
    skus = evidence.get('sku_details') or []
    if len(skus) > 1:
        return ['该类目明确要求商品条码（GTIN），SKU ' + str(row.get('seller_sku') or row.get('sku_id') or index + 1) + ' 未填写' for index, row in enumerate(skus) if not str(row.get('gtin') or '').strip()]
    if common_gtin or (skus and skus[0].get('gtin')):
        return []
    return ['该类目明确要求商品条码（GTIN），当前商品未填写']
