"""Offline offer-search seam over captured 1688 evidence; no I/O except public JSON reads.

prepare(root, draft, provider) returns a NEW draft with evidence.source_offer and
existing input_requests format. It never saves, imports, enqueues, buys, confirms,
copies costs/packages, or invokes a browser/model/network. The existing extension
market_reference (or scoped zying_detail) supplies the MLM lead. Providers return
captured offers, not inferred product facts. See tests/fixtures/offer_candidates.json.
"""
import copy
import json
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path

from .auto_processing_v3 import capture_facts, input_requests
from .procurement_v3 import assess as assess_procurement, digest, offer_id
from .product_match_v3 import assess as assess_product, units
from .source_intake import _contains_forbidden_key, normalize_collection

VERSION = 'OFFER_SEARCH_MLM_V1'
FIELDS = ('weight_g', 'length_cm', 'width_cm', 'height_cm')


def policy(root):
    value = json.loads((Path(root) / 'config/offer_match_policy.json').read_text('utf-8'))
    if value.get('site_id') != 'MLM':
        raise ValueError('offer policy only supports MLM')
    for key in ('min_confidence', 'min_title_similarity', 'missing_image_multiplier',
                'weight_relative_tolerance', 'dimension_relative_tolerance'):
        if not _finite(value.get(key)) or not 0 < value[key] <= 1:
            raise ValueError('invalid offer policy: ' + key)
    weights = value.get('weights') or {}
    if set(weights) != {'title', 'specifications', 'quantity', 'package', 'image'} or any(
            not _finite(v) or v <= 0 for v in weights.values()):
        raise ValueError('invalid offer weights')
    if type(value.get('max_candidates')) is not int or not 1 <= value['max_candidates'] <= 100:
        raise ValueError('invalid candidate bound')
    return value


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class OfferSearchProvider(ABC):
    name = 'abstract'

    @abstractmethod
    def search(self, query):
        """Return {status: READY|BLOCKED_ON_*, candidates: [{capture, sku_identity}]}.

        query contains public market_reference and target_sku_id. Implementations
        must enforce login/access gates before any future authorized transport.
        """
        raise NotImplementedError


class Manual1688Capture(OfferSearchProvider):
    name = 'manual_1688_capture'

    def __init__(self, candidates):
        if not isinstance(candidates, list) or _contains_forbidden_key(candidates):
            raise ValueError('candidate list invalid or contains forbidden fields')
        self.candidates = copy.deepcopy(candidates)

    def search(self, query):
        return {'status': 'READY', 'candidates': copy.deepcopy(self.candidates)}


class Zying1688ImageSearch(OfferSearchProvider):
    """Deliberately transport-free. Login state is an explicit public assertion.

    Read only the sanitized last-result diagnostics, never supplier runtime/vault.
    Existing 400/ACL denial takes precedence over login; do not probe or retry.
    """
    name = 'zying_1688_image_search'

    def __init__(self, root, *, logged_in=False):
        self.root, self.logged_in = Path(root), logged_in is True

    def search(self, query):
        try:
            value = json.loads((self.root / 'data/v3/supplier1688/last-result.json').read_text('utf-8'))
            diagnostic = value.get('diagnostics') or {}
        except (OSError, ValueError, AttributeError):
            diagnostic = {}
        denied = (diagnostic.get('provider_code') == 'gw.APIACLDecline' or
                  diagnostic.get('provider_category') == 'API_PERMISSION_REQUIRED' or
                  diagnostic.get('http_status') in (400, 401, 403))
        status = 'BLOCKED_ON_ACCESS' if denied or self.logged_in else 'BLOCKED_ON_LOGIN'
        reason = ('历史400/ACL拒绝；本次未取得候选，未发请求' if denied else
                  '真实图搜通道未核，当前适配器禁止联网；本次未取得候选' if self.logged_in else
                  '需主人确认登录并提供候选采集；本次未取得，未发请求')
        return {'status': status, 'reason': reason, 'candidates': [], 'request_performed': False}


def _tokens(text):
    text = str(text or '').casefold()
    words = set(re.findall(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*', text))
    for chunk in re.findall(r'[\u4e00-\u9fff]+', text):
        words.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


def _specs(value):
    aliases = {'型号': 'model', 'MODEL': 'model', '颜色': 'color', '材质': 'material'}
    if not isinstance(value, dict):
        raise ValueError('specifications must be an explicit object')
    result = {}
    for key, value in value.items():
        if value is None or not str(value).strip():
            continue
        key, value = aliases.get(str(key), str(key).casefold()), str(value).strip().casefold()
        if key in result and result[key] != value:
            raise ValueError('conflicting specification aliases')
        result[key] = value
    return result


def _quantity(value):
    return int(value) if re.fullmatch(r'[1-9]\d{0,5}', str(value or '')) else None


def _issue(code, field, reason, **kw):
    return {'code': code, 'field': field, 'reason': reason, **kw}


def _lead(draft, sku):
    ev = draft.get('evidence') or {}
    detail = ev.get('zying_detail') or {}
    ref = sku.get('market_reference') or ev.get('market_reference')
    if not ref and detail:
        ref = {'market_item_id': detail.get('sku_id'), 'site_id': detail.get('site_id'),
               'title': detail.get('title'), 'source_url': detail.get('source_url')}
    ref = copy.deepcopy(ref or {})
    if ref.get('site_id') != 'MLM' or not re.fullmatch(r'MLM\d{6,20}', str(ref.get('market_item_id') or '')):
        raise ValueError('同款搜索仅支持明确的墨西哥MLM线索')
    if ref.get('source_sku_id') and str(ref['source_sku_id']) != str(sku.get('sku_id')):
        raise ValueError('market reference belongs to another SKU')
    # A product-level lead cannot silently bind all variants.
    if len(ev.get('sku_details') or []) > 1 and not sku.get('market_reference'):
        raise ValueError('多SKU市场线索须逐SKU绑定')
    identity = sku.get('offer_match_identity') or {}
    specs = _specs(identity.get('specs') or {})
    qty = _quantity(identity.get('units_per_pack') or ref.get('units_per_pack')) or units(ref.get('title'))
    quantities = {_quantity(identity.get('units_per_pack')), _quantity(ref.get('units_per_pack')), units(ref.get('title'))} - {None}
    if len(quantities) > 1:
        raise ValueError('target identity and market pack counts conflict')
    package = identity.get('package') or {}
    if not package and detail.get('sku_id') == ref['market_item_id']:
        package = detail.get('package') or {}
    return ref, specs, qty, package


def _compare(draft, target, ref, specs, qty, package, candidate, capture, source, p):
    identity = (candidate.get('sku_identity') or {}).get(str(source['sku_id'])) or {}
    other_specs = _specs(identity.get('specs') or {})
    other_qty = _quantity(identity.get('units_per_pack')) or units(source.get('variant')) or units(capture['title'])
    issues = []
    quantities = {_quantity(identity.get('units_per_pack')), units(source.get('variant')), units(capture['title'])} - {None}
    if len(quantities) > 1:
        issues.append(_issue('OFFER_QUANTITY_CONFLICT', 'units_per_pack', '候选SKU、标题与明确件数之间冲突'))
    left, right = _tokens(ref.get('title')), _tokens(capture['title'])
    title = len(left & right) / len(left | right) if left and right else None
    if title is None or title < p['min_title_similarity']:
        issues.append(_issue('OFFER_TITLE_LOW', 'title', '标题关键词相似度不足或缺失，需人工核对'))
    shared = set(specs) & set(other_specs)
    if not specs or set(specs) - shared:
        issues.append(_issue('OFFER_SPEC_MISSING', 'specifications', '缺少逐SKU明确规格，不能仅凭标题或图片认定同款'))
    for key in sorted(shared):
        if specs[key] != other_specs[key]:
            issues.append(_issue('OFFER_SPEC_CONFLICT', key, '关键规格冲突',
                                 supplier_value=other_specs[key], reference_value=specs[key]))
    spec_score = sum(specs[k] == other_specs[k] for k in shared) / len(specs) if specs else None
    if not qty or not other_qty:
        issues.append(_issue('OFFER_QUANTITY_MISSING', 'units_per_pack', '缺少销售单位/采购单位明确件数'))
    elif qty != other_qty:
        issues.append(_issue('OFFER_QUANTITY_CONFLICT', 'units_per_pack', '销售与采购件数冲突',
                             supplier_value=other_qty, reference_value=qty))
    # Raw, scoped captured packaging is compared only. No geometry is adopted.
    supplier_pack = source.get('package') or {}
    distances = []
    for key in FIELDS:
        a, b = package.get(key), supplier_pack.get(key)
        if not _finite(a) or not _finite(b) or a <= 0 or b <= 0:
            issues.append(_issue('OFFER_PACKAGE_MISSING', key, '缺少智赢包装或逐采购SKU包装的明确重尺'))
            continue
        relative = abs(a - b) / a
        tolerance = p['weight_relative_tolerance' if key == 'weight_g' else 'dimension_relative_tolerance']
        distances.append(max(0.0, 1 - relative))
        if relative > tolerance + 1e-12:
            issues.append(_issue('OFFER_PACKAGE_CONFLICT', key, '包装差异超过配置容差', supplier_value=b, reference_value=a))
    # Reuse product_match's scope/model/pack checks, forcing pending confirmation.
    pending = {**ref, 'spec_match': 'SOURCE_SELECTED_PENDING_REVIEW',
               'source_sku_id': str(source['sku_id']), 'supplier_offer_id': capture['offer_id'], 'units_per_pack': qty}
    supplier_sku = {**source, 'supplier_facts': {'attributes': {'MODEL': other_specs.get('model')}}}
    temporary = {'evidence': {'supplier_url': capture['source_url'], 'source_title': capture['title'],
                             'sku_details': [supplier_sku], 'supplier_page': {'title': capture['title']}}}
    check = assess_product(temporary, supplier_sku, pending)
    issues.extend(i for i in check['issues'] if i['code'] != 'REFERENCE_SPEC_UNCONFIRMED')
    composition = assess_procurement(draft, target)
    if 'procurement' in target:
        issues.extend(composition['issues'])
        if capture['offer_id'] != offer_id(draft):
            issues.append(_issue('OFFER_COMPOSITION_CONFLICT', 'procurement', '已有采购组成不能自动替换为另一货源'))
    scores = {'title': title, 'specifications': spec_score,
              'quantity': float(qty == other_qty) if qty and other_qty else None,
              'package': sum(distances) / len(FIELDS) if len(distances) == len(FIELDS) else None,
              'image': None}
    weights = {k: p['weights'][k] for k, v in scores.items() if v is not None}
    total = sum(weights.values())
    confidence = (sum(scores[k] * w for k, w in weights.items()) / total if total else 0) * p['missing_image_multiplier']
    confidence = round(confidence, 6)
    if confidence < p['min_confidence']:
        issues.append(_issue('OFFER_CONFIDENCE_LOW', 'confidence', '置信度低于同款候选阈值'))
    conflict = any('CONFLICT' in i['code'] for i in issues)
    return {'offer_id': capture['offer_id'], 'source_sku_id': str(source['sku_id']),
            'spec_id': source.get('spec_id'), 'source_url': capture['source_url'],
            'capture_sha256': digest(capture_facts(capture)), 'confidence': confidence, 'scores': scores,
            'effective_weights': {k: round(w / total, 6) for k, w in weights.items()},
            'image_basis': 'UNAVAILABLE_LOCAL_FEATURES', 'state': 'CONFLICT' if conflict else 'CANDIDATE',
            'status': 'SKIPPED' if issues else 'CANDIDATE', 'issues': issues,
            'requires_human_confirmation': True, 'supplier_dims_validated': False}


def prepare(root, draft, provider):
    """Evaluate each target independently; return a copy, never persist or publish.

    source_offer.rows[].source_offer is selected evidence (or null); candidates
    retain rejected diagnostics. Completion stays BLOCKED_EVIDENCE until review.
    Existing unrelated input_requests and all source/price/packaging facts survive.
    """
    if not isinstance(draft, dict) or _contains_forbidden_key(draft):
        raise ValueError('invalid draft or forbidden fields')
    p = policy(root)
    result = copy.deepcopy(draft)
    ev = result.setdefault('evidence', {})
    previous = ev.get('source_offer')
    if previous and previous.get('version') != VERSION:
        raise ValueError('existing source_offer preserved; explicit migration required')
    rows, all_issues = [], []
    targets = ev.get('sku_details') or []
    target_ids = [str(s.get('sku_id') or '') for s in targets if isinstance(s, dict)]
    for sku in targets:
        if not isinstance(sku, dict):
            all_issues.append(_issue('OFFER_INPUT_INVALID', 'source_offer', '目标SKU结构无效'))
            continue
        sid = str(sku.get('sku_id') or '')
        row = {'sku_id': sid, 'source_offer': None, 'candidates': [], 'issues': []}
        rows.append(row)
        try:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', sid) or target_ids.count(sid) != 1:
                raise ValueError('target SKU must be unique and explicit')
            ref, specs, qty, package = _lead(result, sku)
            response = provider.search({'market_reference': copy.deepcopy(ref), 'target_sku_id': sid})
            if not isinstance(response, dict) or _contains_forbidden_key(response):
                raise ValueError('invalid provider response or forbidden fields')
            status = response.get('status')
            if status in {'BLOCKED_ON_LOGIN', 'BLOCKED_ON_ACCESS'}:
                row['status'] = status
                row['issues'].append(_issue(status, 'source_offer', str(response.get('reason') or status)[:400]))
            elif status != 'READY' or not isinstance(response.get('candidates'), list):
                raise ValueError('invalid provider status/candidate list')
            else:
                candidates = response['candidates']
                if len(candidates) > p['max_candidates']:
                    raise ValueError('candidate list exceeds configured bound')
                for candidate in candidates:
                    try:
                        if not isinstance(candidate, dict):
                            raise ValueError('candidate must be an object')
                        capture = normalize_collection(candidate['capture'])
                        sources = capture['skus']
                        ids = [str(s.get('sku_id') or '') for s in sources]
                        if not ids or not all(ids) or len(ids) != len(set(ids)):
                            raise ValueError('candidate SKU ids missing or duplicated')
                        for source in sources:
                            row['candidates'].append(_compare(result, sku, ref, specs, qty, package, candidate, capture, source, p))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        row['candidates'].append({'status': 'SKIPPED', 'state': 'CANDIDATE', 'confidence': 0,
                                                  'issues': [_issue('OFFER_CAPTURE_INVALID', 'source_offer', '候选采集结构无效，需补采；已继续其它候选')]})
                eligible = sorted((c for c in row['candidates'] if c['status'] == 'CANDIDATE'),
                                  key=lambda c: (-c['confidence'], c['offer_id'], c['source_sku_id']))
                row['issues'].extend({**i, 'reason': f"候选 {c.get('offer_id', '未核')}/{c.get('source_sku_id', '未核')}：{i['reason']}"}
                                     for c in row['candidates'] if c['status'] == 'SKIPPED' for i in c['issues'])
                if eligible:
                    row['source_offer'] = copy.deepcopy(eligible[0])
                    row['status'] = 'CANDIDATE'
                    row['issues'].append(_issue('OFFER_HUMAN_CONFIRMATION_REQUIRED', 'source_offer', '自动结果仅为同款候选，需人工确认逐SKU规格与销售单位'))
                else:
                    row['status'] = 'SKIPPED'
                    if not row['issues']:
                        row['issues'].append(_issue('OFFER_CANDIDATES_MISSING', 'source_offer', '本次未取得候选列表，请补采'))
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            row['status'] = 'SKIPPED'
            row['issues'].append(_issue('OFFER_INPUT_INVALID', 'source_offer', '线索/候选结构或SKU绑定无效，请补齐后重试；已继续其它SKU'))
        all_issues.extend({**i, 'sku_id': sid} for i in row['issues'])
    if not rows:
        all_issues.append(_issue('OFFER_SKUS_MISSING', 'source_offer', '缺少明确的目标SKU列表'))
    report = {'version': VERSION, 'provider': provider.name, 'site_id': 'MLM', 'policy': copy.deepcopy(p),
              'policy_sha256': digest(p), 'status': 'BLOCKED_EVIDENCE', 'rows': rows,
              'supplier_facts_modified': False, 'network_requests_performed': False}
    ev['source_offer'] = report
    # Keep requests owned by other stages. Rebuild only this adapter's own rows.
    result['input_requests'] = [r for r in result.get('input_requests') or [] if r.get('origin') != VERSION]
    result['input_requests'].extend({**r, 'origin': VERSION} for r in input_requests([], all_issues))
    return result
