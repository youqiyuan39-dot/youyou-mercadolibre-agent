"""Transport-free MLM budget pricing seam. prepare returns a copy, never saves.

Captured result contract: PRICING_RESULT_V1, input_sha256 from inputs(), currency,
margin_basis=BUYER_PRICE, profit_basis=UNIT_CONTRIBUTION_AFTER_RESERVES,
buyer_price and unit_contribution. Unknown provider wrappers stay unavailable.
Legacy zying_profit_reference is retained as reference only (basis unverified).
No provider client, paid operation, price write or publication is implemented.
"""
import copy
import re
from decimal import Decimal, InvalidOperation

from . import autopricing_v3 as pricing
from . import market_follow_v3
from .auto_processing_v3 import input_requests
from .procurement_v3 import digest
from .source_intake import _contains_forbidden_key

VERSION = 'PRICING_RESULT_MLM_V1'
FORMULA = 'UNIT_CONTRIBUTION_AFTER_RESERVES'


def contribution(root, draft):
    """Local intake estimate using the existing solver; explicit all-in shipping, no reference profit."""
    ev=draft.get('evidence') or {}; detail=ev.get('zying_detail') or {}
    sid=detail.get('sku_id'); skus=[s for s in ev.get('sku_details',[]) if s.get('sku_id')==sid]
    sku=skus[0] if len(skus)==1 else {}
    p=pricing.policy(root); issues=[]; values={}
    def need(field, value, positive=False):
        try: values[field]=number(value,field,positive=positive)
        except Missing:
            issues.append({'sku_id':sid,'code':'CONTRIBUTION_INPUT_MISSING','field':field,
                           'reason':'本项目贡献利润待补：'+field})
    need('buyer_price_mxn',(detail.get('buyer_price') or {}).get('MXN'),True)
    for field in ('purchase_cost_cny','domestic_shipping_cny','shipping_cost_cny','cny_per_mxn'):
        need(field,sku.get(field),field=='cny_per_mxn')
    source=str(sku.get('fx_source') or '').strip()
    if not source:
        issues.append({'sku_id':sid,'code':'CONTRIBUTION_INPUT_MISSING','field':'fx_source',
                       'reason':'本项目贡献利润待补：人民币/墨西哥比索汇率来源及日期（不联网取值）'})
    try:
        decision=pricing.margin_policy(p,'BUYER_PRICE')
        if decision['status']!='CONFIRMED' or decision['minimum_margin_operator']!='GT' or Decimal(decision['minimum_margin_pct'])!=10:
            raise ValueError('现行政策不是已确认的售价严格超过10%')
        for field in ('other_cny','packaging_material_cny','packing_service_cny'):
            need(field,p.get(field))
        for field in ('ads_net_pct','tax_reserve_net_pct','returns_base_pct'):
            pricing.pct(p,field)
        if pricing.pct(p,'ads_net_pct')+pricing.pct(p,'tax_reserve_net_pct')>=1: raise ValueError('广告税费预留比例无效')
        commission=number(detail.get('commission_pct') if detail.get('commission_pct') is not None else p.get('commission_pct',18),'commission_pct')
        if commission>=100: raise ValueError('佣金比例无效')
    except (ValueError,TypeError,KeyError):
        issues.append({'sku_id':sid,'code':'CONTRIBUTION_INPUT_MISSING','field':'autopricing_policy',
                       'reason':'本项目贡献利润：政策或佣金配置待核'})
    requests=[{**r,'origin':'CONTRIBUTION_INPUT','blocking':True} for r in input_requests([],issues)]
    result={'decision':'PENDING','currency':'MXN','margin_basis':'BUYER_PRICE',
            'minimum_margin_operator':'GT','minimum_margin_pct':'10','input_requests':requests,
            'reference_profit_used':False,'requires_budget_review':True,
            'shipping_basis':'shipping_cost_cny=跨境及尾程合计；另保留policy打包服务/包材/其他/广告/税费/退货预留',
            'fx_source':source,'policy_sha256':digest(p)}
    if issues: return result
    v=values; gross=v['buyer_price_mxn']; net=gross*(1-commission/100)
    if net<=0: return result
    context={'sku_id':sid,'currency':'MXN','buyer_price':str(gross),'net_proceeds':str(net),
             'purchase_cny':str(v['purchase_cost_cny']),'domestic_cny':str(v['domestic_shipping_cny']),
             'forwarder_cny':str(v['shipping_cost_cny']+v['packing_service_cny']),
             'packaging_cny':str(v['packaging_material_cny']),'cny_per_currency':str(v['cny_per_mxn']),
             'policy':{k:p[k] for k in ('other_cny','ads_net_pct','tax_reserve_net_pct','returns_base_pct',
                                      'minimum_margin_pct','minimum_margin_operator','margin_policy_status','confirmed_margin_basis')},
             'policy_sha256':digest(p),'fx_source':source,'commission_pct':str(commission)}
    solved=evaluate(context)
    return {**result,'decision':'PASS' if solved['floor_passed'] else 'BELOW_FLOOR',
            'unit_contribution':solved['unit_contribution'],'sales_margin_pct':solved['sales_margin_pct'],
            'inputs':context,'input_sha256':solved['input_sha256']}


class Missing(ValueError):
    def __init__(self, field, reason):
        self.field = field
        super().__init__(reason)


def number(value, field, *, positive=False, signed=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        n = Decimal(str(value))
        if not n.is_finite() or (not signed and n < 0) or (positive and n <= 0):
            raise ValueError()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise Missing(field, '缺少或无效数值：' + field) from None


def inputs(root, draft, sku, budget):
    """Build scoped public inputs and hash; no source facts are changed.

    Slice2 currently carries identity/hash but no purchase price. A later captured
    selected source_offer may include purchase_cost_cny (per selling unit) and
    cost_basis=PER_SELLING_UNIT. Without these, request evidence, never borrow
    calculator prefill or draft purchase price. budget is keyed by target SKU.
    """
    sid = str(sku.get('sku_id') or '')
    ev = draft.get('evidence') or {}
    detail = ev.get('zying_detail') or {}
    ref = sku.get('market_reference') or (ev.get('market_reference') if len(ev.get('sku_details') or []) == 1 else {}) or {}
    market_id = ref.get('market_item_id') or (sid if sid == detail.get('sku_id') else None)
    if (detail.get('site_id') != 'MLM' or detail.get('source') != 'zying_detail' or
            detail.get('sku_id') != market_id or not re.fullmatch(r'MLM\d{6,20}', str(market_id or ''))):
        raise Missing('zying_detail', '缺少逐SKU绑定的MLM智赢详情证据')
    if len(ev.get('sku_details') or []) > 1 and not sku.get('market_reference'):
        raise Missing('market_reference', '多SKU须显式绑定智赢参考')
    if ref.get('source_sku_id') and str(ref['source_sku_id']) != sid:
        raise Missing('market_reference', '市场参考SKU冲突')
    if any(s.get('site_id') != 'MLM' for s in (draft.get('payload') or {}).get('sites_to_sell') or []):
        raise Missing('site_id', '只支持MLM')
    offers = [r for r in (ev.get('source_offer') or {}).get('rows') or [] if str(r.get('sku_id')) == sid]
    offer = offers[0].get('source_offer') if len(offers) == 1 else None
    if (not isinstance(offer, dict) or offer.get('status') not in ('CANDIDATE', 'CONFIRMED') or
            offer.get('state') == 'CONFLICT' or not offer.get('source_sku_id') or
            not offer.get('capture_sha256') or not offer.get('source_url') or
            offer.get('cost_basis') != 'PER_SELLING_UNIT'):
        raise Missing('source_offer.purchase_cost_cny', '切片2采购证据缺逐销售单位成本及来源；请补采')
    purchase = number(offer.get('purchase_cost_cny'), 'source_offer.purchase_cost_cny', positive=True)
    if 'procurement' in sku:
        from .procurement_v3 import assess
        if assess(draft, sku)['issues']:
            raise Missing('procurement', '已有采购组成待核对，不能改用单一报价')
    package = detail.get('package') or {}
    if package.get('source') != 'zying_detail' or detail.get('unshippable'):
        raise Missing('zying_detail.package', '包装证据来源无效或不可发')
    pack = {k: str(number(package.get(k), 'zying_detail.package.' + k, positive=True))
            for k in ('length_cm', 'width_cm', 'height_cm', 'weight_g')}
    if Decimal(pack['weight_g']) > 15000:
        raise Missing('weight_g', '不可发：超过15kg')
    p = pricing.policy(root)
    decision_policy = pricing.margin_policy(p, 'BUYER_PRICE')
    if (decision_policy['status'] != 'CONFIRMED' or p.get('minimum_margin_operator') != 'GT' or
            number(p.get('minimum_margin_pct'), 'minimum_margin_pct') != 10):
        raise Missing('autopricing_policy', '策略须确认买家售价严格大于10%，不自动覆盖策略')
    currency = budget.get('currency')
    if currency not in ('USD', 'MXN'):
        raise Missing('currency', '须提供USD或MXN预算币种')
    gross = number((detail.get('buyer_price') or {}).get(currency), 'buyer_price', positive=True)
    fx = number(budget.get('cny_per_currency'), 'cny_per_currency', positive=True)
    domestic = number(budget.get('domestic_shipping_cny'), 'domestic_shipping_cny')
    default = number(p.get('commission_pct', '18'), 'commission_pct')
    captured = detail.get('commission_pct')
    commission = number(captured, 'commission_pct') if captured is not None else default
    if commission >= 100 or default >= 100:
        raise Missing('commission_pct', '佣金须小于100%')
    commission_info = {'default_pct': str(default), 'captured_pct': str(commission) if captured is not None else None,
                       'selected_pct': str(commission), 'conflict': captured is not None and commission != default,
                       'source': 'zying_detail' if captured is not None else 'policy' if 'commission_pct' in p else 'USER_18_PERCENT_BUDGET',
                       'rule': 'captured_over_default; net_proceeds never charged twice'}
    net_raw = (detail.get('net_proceeds') or {}).get(currency)
    if net_raw is not None:
        net = number(net_raw, 'net_proceeds', positive=True)
        net_basis = 'CAPTURED_NET_NO_REPEAT_COMMISSION'
    else:
        shipping = number(budget.get('platform_shipping'), 'platform_shipping')
        net = gross * (1 - commission / 100) - shipping
        net_basis = 'GROSS_MINUS_COMMISSION_AND_EXPLICIT_SHIPPING'
    if not 0 < net <= gross:
        raise Missing('net_proceeds', '净回款必须大于0且不超过买家售价')
    # Reuse package/forwarder arithmetic on an ephemeral object, never SKU facts.
    forwarder = pricing.forwarder({'package': pack}, {'evidence': {}}, p)
    result = {'sku_id': sid, 'market_item_id': market_id, 'currency': currency, 'buyer_price': str(gross),
              'net_proceeds': str(net), 'net_basis': net_basis, 'purchase_cny': str(purchase),
              'domestic_cny': str(domestic), 'cny_per_currency': str(fx), 'forwarder_cny': str(forwarder),
              'packaging_cny': str(number(p.get('packaging_material_cny'), 'packaging_material_cny')),
              'package': pack, 'package_evidence_sha256': digest(detail), 'source_offer_sha256': digest(offer),
              'commission': commission_info,
              'policy': {k: p[k] for k in ('minimum_margin_pct', 'minimum_margin_operator', 'margin_policy_status',
                                          'confirmed_margin_basis', 'ads_net_pct', 'tax_reserve_net_pct',
                                          'returns_base_pct', 'other_cny', 'packaging_selection') if k in p},
              'policy_sha256': digest(p)}
    return result


def adapt_result(value, context):
    """Parse only explicit comparable captured results; do not trust displayed %.

    A future real capture must map its actual fields and verified methodology to
    this versioned contract. A fixture asserting the contract is not a live test.
    """
    if not isinstance(value, dict) or _contains_forbidden_key(value):
        return None
    if (value.get('schema') != 'PRICING_RESULT_V1' or value.get('site_id') != 'MLM' or
            value.get('sku_id') != context['sku_id'] or value.get('currency') != context['currency'] or
            value.get('input_sha256') != digest(context) or value.get('margin_basis') != 'BUYER_PRICE' or
            value.get('profit_basis') != FORMULA or value.get('basis_verified') is not True or
            value.get('provider') not in ('zying_ai', '1688_pricing', 'zying_profit_reference')):
        return None
    try:
        gross = number(value.get('buyer_price'), 'result.buyer_price', positive=True)
        profit = number(value.get('unit_contribution'), 'result.unit_contribution', signed=True)
        if gross != Decimal(context['buyer_price']) or profit > gross:
            return None
    except Missing:
        return None
    return {'buyer_price': str(gross), 'unit_contribution': str(profit),
            'provider': value['provider'], 'result_sha256': digest(value)}


def evaluate(context, captured=None):
    row = adapt_result(captured, context)
    if row is None:
        c, p = context, context['policy']
        solved = market_follow_v3.solve(c['buyer_price'], c['net_proceeds'], c['purchase_cny'],
                                       c['domestic_cny'], c['forwarder_cny'], c['packaging_cny'], c['cny_per_currency'], p)
        # Decision uses exact Decimal arithmetic, before solve's display rounding.
        base = sum(Decimal(c[k]) for k in ('purchase_cny', 'domestic_cny', 'forwarder_cny', 'packaging_cny')) + number(p.get('other_cny'), 'other_cny')
        profit = Decimal(c['net_proceeds']) * (1 - pricing.pct(p, 'ads_net_pct') - pricing.pct(p, 'tax_reserve_net_pct')) - base * (1 + pricing.pct(p, 'returns_base_pct')) / Decimal(c['cny_per_currency'])
        row = {'buyer_price': c['buyer_price'], 'unit_contribution': str(profit), 'provider': 'LOCAL_FORMULA',
               'fallback_reason': 'CAPTURED_RESULT_UNAVAILABLE_OR_BASIS_UNVERIFIED',
               'formula': solved['formula']}
    margin = Decimal(row['unit_contribution']) / Decimal(row['buyer_price']) * 100
    passed = margin > 10
    return {**row, 'sku_id': context['sku_id'], 'currency': context['currency'], 'sales_margin_pct': str(margin),
            'floor_passed': passed, 'decision': 'PASS_FLOOR' if passed else 'BELOW_FLOOR',
            'status': 'BUDGET_ESTIMATE' if passed else 'SKIPPED', 'margin_basis': 'BUYER_PRICE',
            'minimum_margin_pct': '10', 'minimum_margin_operator': 'GT', 'input_sha256': digest(context),
            'inputs': context, 'requires_budget_review': True, 'price_facts_modified': False}


def prepare(root, draft, budgets, captured_results=None):
    """Pure local stage API: budgets/results keyed by SKU; skips enter input_requests.

    No production queue/HTTP wiring: preserves Slice2's pluggable boundary. Only
    evidence.pricing_result and this stage's requests are replaced in the copy.
    """
    if not isinstance(draft, dict) or _contains_forbidden_key(draft):
        raise ValueError('invalid draft or forbidden fields')
    result = copy.deepcopy(draft)
    ev = result.setdefault('evidence', {})
    rows, issues = [], []
    targets = ev.get('sku_details') or []
    ids = [str(s.get('sku_id') or '') for s in targets if isinstance(s, dict)]
    for sku in targets:
        sid = str(sku.get('sku_id') or '') if isinstance(sku, dict) else ''
        try:
            if not sid or ids.count(sid) != 1:
                raise Missing('sku_id', 'SKU编号为空或重复')
            context = inputs(root, result, sku, budgets.get(sid) or {})
            captured = (captured_results or {}).get(sid)
            if captured is None:
                captured = ev.get('zying_profit_reference')
            row = evaluate(context, captured)
            if not row['floor_passed']:
                issues.append({'sku_id': sid, 'code': 'PRICING_BELOW_FLOOR', 'field': 'pricing_result',
                               'reason': '贡献利润÷买家售价未严格超过10%；自动跳过，请补充成本或核价证据'})
        except (Missing, ValueError, TypeError, KeyError, AttributeError) as exc:
            row = {'sku_id': sid, 'status': 'SKIPPED', 'decision': 'BLOCKED_EVIDENCE', 'floor_passed': None}
            issues.append({'sku_id': sid, 'code': 'PRICING_INPUT_MISSING', 'field': getattr(exc, 'field', 'pricing_result'),
                           'reason': str(exc)[:300]})
        rows.append(row)
    if not rows:
        issues.append({'code': 'PRICING_INPUT_MISSING', 'field': 'sku_details', 'reason': '缺少SKU列表'})
    ev['pricing_result'] = {'version': VERSION, 'state': 'BLOCKED_EVIDENCE' if issues else 'BUDGET_ESTIMATE',
                            'rows': rows, 'requires_budget_review': True, 'network_requests_performed': False,
                            'live_provider_status': 'BLOCKED_ON_ACCESS',
                            'live_provider_reason': '无真实核价样本及已核方法映射；本片无网络传输，不触发AI核价',
                            'price_facts_modified': False}
    result['input_requests'] = [r for r in result.get('input_requests') or [] if r.get('origin') != VERSION]
    result['input_requests'].extend({**r, 'origin': VERSION, 'blocking': True} for r in input_requests([], issues))
    return result
