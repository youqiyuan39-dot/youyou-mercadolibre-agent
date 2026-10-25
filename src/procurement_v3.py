"""Sales-unit composition over existing supplier SKU evidence; no network or inference.

Optional sku.procurement extends the current SKU record. Source prices/images stay
in evidence.supplier_page.skus, not in a second product database. The initial
adapter supports multiple SKUs from the captured offer; another offer is an
explicit unsupported input, never resolved by a fuzzy match or implicit price.
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

VERSION = 'SALES_UNIT_COMPONENTS_20261018_V1'
CONFIRMED = {'HUMAN_CONFIRMED', 'USER_CONFIRMED'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9]\d{0,5}', str(value)):
        raise ValueError(label + '必须为1至999999的整数')
    return int(value)


def number(value, label, zero=False):
    try:
        n = Decimal(str(value)) if not isinstance(value, bool) else Decimal('NaN')
    except (InvalidOperation, ValueError):
        raise ValueError(label + '不是有效金额')
    if not n.is_finite() or n < 0 or (not zero and n == 0) or n > Decimal('1000000000'):
        raise ValueError(label + '必须是范围内的有限金额')
    return n


def offer_id(draft):
    ev = draft.get('evidence') or {}
    m = re.search(r'/offer/(\d+)\.html', str(ev.get('supplier_url') or draft.get('source_url') or ''))
    return m[1] if m else None


def source_signature(draft, source):
    # AI content, editorial locks, and current sales prices are not source facts.
    return digest({'offer_id': offer_id(draft), 'source': {k: source.get(k) for k in
                   ('sku_id', 'spec_id', 'variant', 'price', 'inventory', 'pricing_tiers')}})


def plan_signature(plan):
    return digest({k: v for k, v in plan.items() if k not in {'locked', 'updated_at'}})


def assess(draft, sku):
    """Never writes into the draft, procurement plan, package, or supplier record."""
    plan = sku.get('procurement')
    result = {'version': VERSION, 'sku_id': str(sku.get('sku_id') or ''),
              'state': 'LEGACY_SINGLE_SOURCE', 'components': [], 'issues': [],
              'purchase_cost_cny': None, 'units_per_sale': None, 'distinct_source_skus': None}
    if 'procurement' not in sku:
        return result
    issues = result['issues']

    def issue(code, reason, **kw):
        issues.append({'code': code, 'field': 'procurement', 'reason': reason, **kw})

    if not isinstance(plan, dict) or not isinstance(plan.get('components'), list) or not 1 <= len(plan['components']) <= 30:
        issue('COMPOSITION_INVALID', '采购组成需包含1至30项明确采购SKU')
        result['state'] = 'NEEDS_INPUT'
        return result
    result['signature'] = plan_signature(plan)
    if plan.get('confirmation') not in CONFIRMED or not plan.get('confirmation_source'):
        issue('COMPOSITION_UNCONFIRMED', '销售单位与采购组成尚未确认，需核对规格、功能、材质及数量')
    supplier = (draft.get('evidence') or {}).get('supplier_page') or {}
    pool = supplier.get('procurement_skus') or supplier.get('skus') or []
    if not isinstance(pool,list) or any(not isinstance(r,dict) for r in pool):
        issue('COMPOSITION_SOURCE_INVALID','原始采购SKU记录结构无效，不能继续核价')
        result['state']='NEEDS_INPUT'
        return result
    rows, seen, total, units = [], set(), Decimal(0), 0
    for component in plan['components']:
        try:
            if not isinstance(component, dict):
                raise ValueError('采购组成项不是对象')
            sid = str(component.get('source_sku_id') or '')
            source_offer = str(component.get('source_offer_id') or '')
            if not source_offer or source_offer != offer_id(draft):
                raise ValueError('本版仅解析当前已采集货源；其他货源需接入对应原始记录，不能套用当前价格')
            if not sid or sid in seen:
                raise ValueError('采购SKU为空或重复；同款多件应填写数量，不能重复列项')
            seen.add(sid)
            candidates = [r for r in pool if str(r.get('sku_id')) == sid]
            if len(candidates) != 1:
                raise ValueError('采购SKU在原始采集记录中没有唯一对应')
            source = candidates[0]
            if component.get('source_signature') != source_signature(draft, source):
                raise ValueError('采购规格或报价已变化，原确认失效；保留原关系等待重核')
            quantity = integer(component.get('quantity'), '每个销售单位的采购数量')
            unit_count = integer(component.get('units_per_purchase'), '每个采购单位所含件数')
            order_qty = integer(component.get('order_quantity'), '核价采购数量')
            minimum = integer(component.get('minimum_order_quantity'), '起订量')
            if order_qty < max(quantity, minimum) or order_qty % quantity:
                raise ValueError('核价采购数量未满足起订量或不是销售单位组成数量的整数倍')
            basis = component.get('price_basis') or {}
            if not isinstance(basis,dict):raise ValueError('采购报价依据结构无效')
            if (basis.get('currency') != 'CNY' or basis.get('scope') != 'PER_PROCUREMENT_UNIT'
                    or not basis.get('unit') or not basis.get('source') or not basis.get('observed_at')):
                raise ValueError('需明确采购报价的币种、计价单位、用途、来源及时间')
            if not component.get('quantity_source'):
                raise ValueError('缺少起订量及采购单位件数的依据')
            price = number(source.get('price'), '原始分规格报价')
            tiers = source.get('pricing_tiers') or []
            if tiers:
                if not isinstance(tiers,list) or any(not isinstance(t,dict) for t in tiers):
                    raise ValueError('采购阶梯报价结构无效')
                if basis.get('tier_scope') != 'PER_SKU':
                    raise ValueError('阶梯价按SKU还是整单计量尚未确认')
                normalized = [(integer(t.get('min_quantity'), '阶梯量'), number(t.get('price'), '阶梯报价')) for t in tiers]
                if len({q for q, _ in normalized}) != len(normalized):
                    raise ValueError('同一阶梯数量存在重复报价')
                applicable = [(q, p) for q, p in normalized if q <= order_qty]
                if not applicable:
                    raise ValueError('采购数量未达到任何有效阶梯')
                price = max(applicable, key=lambda x: x[0])[1]
            stock = source.get('inventory')
            if stock is not None and number(stock, '供应商库存', zero=True) < order_qty:
                raise ValueError('当前库存少于核价采购数量')
            subtotal = price * quantity
            total += subtotal
            units += quantity * unit_count
            rows.append({'source_offer_id': source_offer, 'source_sku_id': sid, 'spec_id': source.get('spec_id'),
                         'variant': source.get('variant'), 'quantity': quantity, 'units_per_purchase': unit_count,
                         'order_quantity': order_qty, 'unit_price_cny': str(price), 'subtotal_cny': str(subtotal),
                         'source_signature': component['source_signature']})
        except (ValueError, TypeError, KeyError) as exc:
            issue('COMPOSITION_COMPONENT_INVALID', str(exc))
    batches = {r['order_quantity'] // r['quantity'] for r in rows}
    if len(batches) > 1:
        issue('COMPOSITION_ORDER_QUANTITY_CONFLICT', '不同组件核价数量对应的销售套数不一致，阶梯价与运费口径无法对齐')
    try:
        if integer(plan.get('units_per_sale'), '销售单位总件数') != units:
            issue('COMPOSITION_UNITS_CONFLICT', '销售件数与采购组成数量不一致')
    except ValueError as exc:
        issue('COMPOSITION_UNITS_CONFLICT', str(exc))
    # Shipping remains a per-sales-unit allocation, separate from goods subtotal.
    freight = plan.get('freight_allocation') or {}
    if not isinstance(freight,dict):freight={}
    if freight.get('basis') != 'PER_SALES_UNIT' or not freight.get('source'):
        issue('COMPOSITION_FREIGHT_UNCONFIRMED', '需确认国内运费按销售单位分摊；不能对每个SKU重复计入整单运费')
    package = sku.get('package') or {}
    if package.get('sales_unit_signature') != result['signature'] or package.get('scope') != 'SALES_UNIT':
        issue('COMPOSITION_PACKAGE_UNCONFIRMED', '需取得该销售组成的实际包装，不能相加组件尺寸或沿用单件包装')
    result.update(components=rows, units_per_sale=units, distinct_source_skus=len(rows),
                  state='NEEDS_INPUT' if issues else 'CONFIRMED',
                  purchase_cost_cny=str(total) if not issues else None,
                  price_kind='SUPPLIER_QUOTE_NOT_ORDER_SETTLEMENT')
    return result


def purchase_cost(draft, sku):
    if 'procurement' not in sku:
        return sku.get('purchase_cost_cny')
    result = assess(draft, sku)
    if result['issues']:
        raise ValueError('采购组成待核对：' + result['issues'][0]['reason'])
    return result['purchase_cost_cny']


def blocking_issues(draft):
    return ['SKU ' + str(s.get('sku_id')) + '：' + i['reason']
            for s in (draft.get('evidence') or {}).get('sku_details') or []
            for i in assess(draft, s)['issues']]


def image_scope_issues(draft):
    groups = ((draft.get('evidence') or {}).get('image_review') or {}).get('sku_listing_galleries') or []
    issues = []
    for sku in (draft.get('evidence') or {}).get('sku_details') or []:
        if 'procurement' not in sku:continue
        group = next((g for g in groups if str(g.get('sku_id')) == str(sku.get('sku_id'))), {})
        if not isinstance(sku['procurement'],dict) or group.get('sales_unit_signature') != plan_signature(sku['procurement']) or not group.get('gallery'):
            issues.append('SKU ' + str(sku.get('sku_id')) + '：发布图片尚未核对销售组成；单件图不能冒充多件或混合套装图')
    return issues
