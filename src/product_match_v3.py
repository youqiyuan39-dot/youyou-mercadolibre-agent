"""Check an existing market lead against procurement evidence; never invent a match.

No marketplace adapter, credentials, LLM or supplier-fact writes belong here.
"""
import copy
import re

VERSION = 'PRODUCT_MATCH_20261018_V2_COMPONENTS'
HUMAN = {'HUMAN_CONFIRMED_SAME_SKU', 'CONFIRMED_SAME_SKU'}


def units(text):
    text = str(text or '')
    if re.search(r'单个装|单只装|单件装', text):
        return 1
    match = re.search(r'(?:paquete\s+de|pack\s+(?:of|de)|set\s+(?:of\s+)?|套装数量\s*[:：]?)\s*(\d+)|\b(\d+)\s*(?:piezas|pcs|pzs|只装|件装|个装)', text, re.I)
    return int(next(v for v in match.groups() if v)) if match else None


def assess(draft, sku, ref=None):
    ev = draft.get('evidence') or {}
    own_ref = sku.get('market_reference')
    ref = ref if ref is not None else own_ref or ev.get('market_reference')
    supplier = ev.get('supplier_page') or {}
    attributes = (sku.get('supplier_facts') or {}).get('attributes') or {}
    if len(ev.get('sku_details') or []) == 1:
        attributes = {**(supplier.get('attributes') or {}), **attributes}
    result = {'version': VERSION, 'sku_id': str(sku.get('sku_id') or ''),
              'supplier_url': ev.get('supplier_url') or draft.get('source_url'),
              'market_item_id': (ref or {}).get('market_item_id'),
              'original_confirmation': (ref or {}).get('spec_match'),
              'state': 'NOT_LINKED', 'issues': [], 'comparisons': []}
    issues = result['issues']
    from .procurement_v3 import assess as assess_composition
    composition = assess_composition(draft, sku)
    if 'procurement' in sku:
        result['procurement'] = composition
        issues.extend(composition['issues'])
    if not ref:
        if issues:
            result.update(state='CANDIDATE', next_action='CONFIRM_PROCUREMENT_COMPOSITION')
        return result
    def issue(code, field, reason, **values):
        issues.append({'code': code, 'field': field, 'reason': reason, **values})
    original, final = ref.get('market_item_id'), ref.get('resolved_item_id')
    if final and final != original:
        issue('REFERENCE_REDIRECT_CONFLICT', 'market_item_id', '最终刊登编号与原市场商品不同，不能合并其参数和价格', supplier_value=original, reference_value=final)
    source_id = str(ref.get('source_sku_id') or '')
    if source_id and source_id != result['sku_id']:
        issue('REFERENCE_SKU_SCOPE_CONFLICT', 'source_sku_id', '参考证据属于另一采购SKU', supplier_value=result['sku_id'], reference_value=source_id)
    source_offer = re.search(r'/offer/(\d+)', str(result['supplier_url'] or ''))
    bound_offer = ref.get('supplier_offer_id')
    if source_offer and bound_offer and str(bound_offer) != source_offer[1]:
        issue('REFERENCE_OFFER_SCOPE_CONFLICT', 'supplier_offer_id', '参考证据属于另一采购商品，不能只凭相同SKU编号复用', supplier_value=source_offer[1], reference_value=bound_offer)
    model = str(attributes.get('型号') or attributes.get('MODEL') or '')
    models = lambda text: set(re.findall(r'(?<![A-Z0-9])[A-Z]{1,8}[-_]?\d{2,}[A-Z0-9]*', str(text).upper()))
    left, right = models(model), models(ref.get('title') or '')
    if left and right:
        result['comparisons'].append({'field': 'model', 'supplier_value': model, 'reference_value': ref.get('title'), 'equal': bool(left & right)})
        if not left & right:
            issue('REFERENCE_MODEL_CONFLICT', 'model', '参考商品型号与当前货源不符', supplier_value=model, reference_title=ref.get('title'))
    source_units = next((str(attributes[k]).strip() for k in ('每包数量', '每套数量', '套装数量', 'UNITS_PER_PACK') if attributes.get(k) is not None), None)
    source_units = int(source_units) if source_units and re.fullmatch(r'\d+', source_units) else units(sku.get('variant')) or units(supplier.get('title') or ev.get('source_title'))
    if composition['state'] == 'CONFIRMED':
        source_units = composition['units_per_sale']
    market_units = ref.get('units_per_pack')
    market_units = int(market_units) if re.fullmatch(r'[1-9]\d*', str(market_units or '')) else units(ref.get('title'))
    result['comparisons'].append({'field': 'units_per_pack', 'supplier_value': source_units, 'reference_value': market_units, 'equal': source_units == market_units if source_units and market_units else None})
    if source_units and market_units and source_units != market_units:
        issue('REFERENCE_PACK_CONFLICT', 'units_per_pack', '参考商品套装数量与当前货源不符', supplier_value=source_units, reference_value=market_units)
    conflict = bool(issues)
    if not own_ref and ref is ev.get('market_reference') and len(ev.get('sku_details') or []) > 1:
        issue('REFERENCE_SKU_UNBOUND', 'source_sku_id', '商品级市场线索尚未对应具体采购SKU，不能继承给全部规格')
    confirmed = ref.get('spec_match') in HUMAN
    if ref.get('spec_match') == 'VERIFIED_SAME_SKU':
        proof = ref.get('identity_evidence') or []
        # An automatic flag alone is not evidence; all recorded identity dimensions
        # must have two explicit equal values and source locators.
        required = ref.get('required_identity_fields') or []
        supported = {p.get('field') for p in proof if isinstance(p, dict) and p.get('supplier_value') is not None and p.get('supplier_value') == p.get('market_value') and p.get('supplier_source') and p.get('market_source')}
        snapshot = (ev.get('task_evidence') or {}).get('source_sha256')
        confirmed = (bool(required) and {'model','units_per_pack'}.issubset(required)
                     and set(required).issubset(supported) and source_id == result['sku_id']
                     and bool(snapshot) and ref.get('source_capture_sha256') == snapshot
                     and bool(left and right and left & right and source_units and source_units == market_units))
    if not confirmed:
        issue('REFERENCE_SPEC_UNCONFIRMED', 'spec_match', '市场线索尚未确认同规格、同数量和套装；相似标题或图片不能确认同款', reference_value=ref.get('spec_match'))
    result['state'] = 'CONFLICT' if conflict else 'CONFIRMED' if confirmed and not issues else 'CANDIDATE'
    result['next_action'] = 'RESELECT_MARKET_OR_PROCUREMENT_SKU' if conflict else 'CHECK_IDENTITY_FIELDS' if issues else 'KEEP_CONFIRMED_MAPPING'
    return result


def refresh(draft):
    ev = draft.setdefault('evidence', {})
    rows = [assess(draft, sku) for sku in ev.get('sku_details') or []]
    report = {'version': VERSION, 'rows': rows, 'supplier_facts_modified': False,
              'state': 'CONFLICT' if any(r['state'] == 'CONFLICT' for r in rows) else 'CANDIDATE' if any(r['state'] == 'CANDIDATE' for r in rows) else 'CHECKED'}
    ev['product_matching_review'] = copy.deepcopy(report)
    return report


def blocking_issues(draft):
    return [f"SKU {row['sku_id']}：{i['reason']}" for row in refresh(copy.deepcopy(draft))['rows']
            if row['state'] in {'CONFLICT', 'CANDIDATE'} for i in row['issues']]
