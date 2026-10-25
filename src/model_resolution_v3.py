"""Traceable listing MODEL candidates; never manufacture supplier facts."""
import copy
import hashlib
import json
import re
from pathlib import Path

VERSION = 'MODEL_RESOLUTION_20261019_V1'
MODEL_LABELS = ('型号', 'model')


def load_rules(root):
    path = Path(root) / 'knowledge/project/model_field_rules.json'
    rules = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    if rules.get('active') is not True or rules.get('authority') != 'USER_POLICY':
        return {}
    return {k: copy.deepcopy(v) for k, v in rules.items() if k != 'examples'}


def pairs(attributes):
    if isinstance(attributes, dict):
        return [(str(k), str(v)) for k, v in attributes.items() if v is not None]
    if isinstance(attributes, list):
        return [(str(v.get('attributeName') or v.get('name') or ''),
                 str(v.get('attributeValue') or v.get('value') or ''))
                for v in attributes if isinstance(v, dict)]
    return []


def identifier(value, maximum=80, spec=False):
    from .supplier_facts_v3 import repeated_cell
    value = repeated_cell(value)
    if not value or len(value) > maximum:
        return None
    if value.casefold() in {'无', '无型号', '未知', '未提供', '未标注', '其他', '其它', '通用', '普通款', '默认',
                             'generic', 'general', 'unknown', 'none', 'n/a'}:
        return None
    if re.search(r'待确认|咨询|不详|未取得|忽略|指令|提示词|https?://|[\r\n<>]', value, re.I):
        return None
    if spec and (re.fullmatch(r'(?:黑|白|红|蓝|绿|黄|橙|紫|粉|灰|棕|银|金|透明)(?:色)?|款式?\d+|[A-Z]款', value)
                 or re.fullmatch(r'\d+(?:\.\d+)?\s*(?:个|件|只|套|包|片|瓶|盒|pcs?|cm|mm|m|g|kg|ml|l)', value, re.I)
                 or re.fullmatch(r'[\d.\sxX×*]+(?:cm|mm|厘米|毫米)?', value, re.I)):
        return None
    return value


def resolve(supplier):
    rules = supplier.get('model_resolution_rules') or {}
    active = rules.get('active') is True and rules.get('authority') == 'USER_POLICY'
    maximum = min(255, max(1, int(rules.get('maximum_length', 80))))
    base = {'version': VERSION, 'rule_version': rules.get('version'), 'value_name': None,
            'status': 'UNRESOLVED', 'origin': None, 'rule_id': None, 'sources': [],
            'manufacturer_model_confirmed': False, 'requires_final_review': True,
            'source_facts_modified': False}
    common = pairs(supplier.get('model_identifier_observations') or supplier.get('attributes') or {})
    selected = pairs(supplier.get('model_selected_attributes') or {})
    choices = [('EXPLICIT_MODEL', MODEL_LABELS, 'EXPLICIT')]
    if active:
        choices += [('SUPPLIER_ITEM_IDENTIFIER', rules.get('identifier_labels') or [], 'CANDIDATE'),
                    ('SELECTED_SPEC', rules.get('spec_labels') or [], 'CANDIDATE')]
    for rule_id, labels, status in choices:
        allowed = {str(k).casefold() for k in labels}
        selected_rows = [(k, v) for k, v in selected if k.casefold() in allowed]
        rows = selected_rows or [(k, v) for k, v in common if k.casefold() in allowed]
        # Family-wide specifications can describe other variants. Only an exact
        # selected row or a true single-SKU source may supply a spec candidate.
        if rule_id == 'SELECTED_SPEC' and not selected_rows and supplier.get('model_source_sku_count', 1) > 1:
            rows = []
        if rule_id == 'SELECTED_SPEC' and not rows:
            rows = [('selected_variant', str(supplier.get('selected_variant') or ''))]
        valid = []
        for label, quote in rows:
            value = identifier(quote, maximum, spec=rule_id == 'SELECTED_SPEC')
            if value:
                valid.append((label, quote, value))
        if not valid:
            continue
        if len({v.casefold() for _, _, v in valid}) > 1 or any(re.search(r'[/、,，;；|]', v) for _, _, v in valid):
            return {**base, 'status': 'CONFLICT', 'rule_id': rule_id,
                    'sources': [{'label': k, 'quote': q} for k, q, _ in valid],
                    'reason': '多个标识有冲突或混列，未自动选择其中一个'}
        label, quote, value = valid[0]
        return {**base, 'value_name': value, 'status': status, 'rule_id': rule_id,
                'origin': 'SUPPLIER_EXPLICIT_MODEL' if status == 'EXPLICIT' else 'DERIVED_LISTING_IDENTIFIER',
                'manufacturer_model_confirmed': status == 'EXPLICIT',
                'sources': [{'label': k, 'quote': q,
                             'path': '/selected_sku/attributes/' + k if selected_rows else '/attributes/' + k}
                            for k, q, _ in valid],
                'reason': '按供应商明确型号原文提取' if status == 'EXPLICIT' else
                          '缺少明确型号，按供应商货号或所选规格整理为刊登候选；不代表厂家型号已确认'}
    return {**base, 'reason': '没有可用的明确型号、货号或所选规格；未从品牌、包装、竞品或知识库样例猜值'}


def refresh(root, draft):
    from .task_evidence_v3 import source_for
    from .supplier_scope_v3 import attributes_for_sku
    rules = load_rules(root)
    capture, source_path = source_for(root, draft)
    evidence = draft.setdefault('evidence', {})
    supplier = evidence.get('supplier_page') or {}
    raw_skus = capture.get('skus') or supplier.get('skus') or []
    skus = evidence.get('sku_details') or []
    for sku in skus:
        raw = next((s for s in raw_skus if str(s.get('sku_id')) == str(sku.get('sku_id'))), {})
        attrs = attributes_for_sku(capture.get('attributes') or supplier.get('attributes') or {}, sku, skus)
        context = {'attributes': attrs, 'selected_variant': sku.get('variant'),
                   'model_selected_attributes': copy.deepcopy(raw.get('attributes') or {}),
                   'model_resolution_rules': rules, 'model_source_sku_count': len(raw_skus) or len(skus)}
        labels={str(k).casefold() for k in list(MODEL_LABELS)+list(rules.get('identifier_labels') or [])+list(rules.get('spec_labels') or [])}
        context['model_identifier_observations']=[{'name':k,'value':v} for k,v in pairs(capture.get('attributes') or supplier.get('attributes') or {}) if k.casefold() in labels]
        record = resolve(context)
        record.update(sku_id=str(sku.get('sku_id')), source_path=source_path,
                      source_sha256=hashlib.sha256(json.dumps(capture, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        sku['model_resolution'] = record
        sku['model_resolution_rules'] = copy.deepcopy(rules)
        sku['model_selected_attributes'] = context['model_selected_attributes']
        sku['model_source_sku_count'] = context['model_source_sku_count']
        sku['model_identifier_observations'] = context['model_identifier_observations']
    evidence['model_resolution_version'] = VERSION


def attach_context(supplier, sku):
    for key in ('model_resolution_rules', 'model_selected_attributes', 'model_source_sku_count', 'model_identifier_observations'):
        supplier[key] = copy.deepcopy(sku.get(key) or ({} if key != 'model_source_sku_count' else 1))
    supplier['model_resolution'] = resolve(supplier)
    return supplier['model_resolution']
