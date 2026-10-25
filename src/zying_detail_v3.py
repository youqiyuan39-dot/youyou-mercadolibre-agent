"""Offline, MLM-only visible-detail intake. No enqueue, model or remote calls."""
import copy
import json
import math
import re
import uuid
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .source_intake import _contains_forbidden_key
from .auto_processing_v3 import input_requests
from .source_intake import ROOT

SAVE_LOCK = threading.RLock()

FIELDS = ('length_cm', 'width_cm', 'height_cm', 'weight_g')
LABELS = ('dimensions', 'weight', 'volumetric_weight', 'price', 'net_proceeds',
          'seller', 'fulfillment', 'category_path', 'commission', 'listed_at', 'sales', 'title', 'brand')


def public_url(value, detail=False):
    url = urlsplit(str(value or ''))
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('页面或图片必须是无凭据 HTTPS 地址')
    if detail and url.hostname != 'meli.zying.net':
        raise ValueError('只接收智赢详情页')
    # Never persist arbitrary query strings (including SPA query strings).
    fragment = url.fragment.split('?')[0]
    if not re.fullmatch(r'/?[\w/.-]*', fragment):
        fragment = ''
    return urlunsplit((url.scheme, url.netloc, url.path, '', fragment))


def measurement(text, dimension=False):
    text = str(text or '').strip()
    if dimension:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(cm|mm|m|厘米|毫米)', text, re.I)
        if not match:
            return None
        scale = {'cm': 1, 'mm': .1, 'm': 100, '厘米': 1, '毫米': .1}[match[4].lower()]
        values = [float(match[i]) * scale for i in (1, 2, 3)]
    else:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(kg|g|公斤|千克|克)', text, re.I)
        if not match:
            return None
        values = [float(match[1]) * (1000 if match[2].lower() in ('kg', '公斤', '千克') else 1)]
    return values if all(math.isfinite(v) and v > 0 for v in values) else None


def money(text):
    result = {}
    for currency, pattern in (('USD', r'USD|US\$|美元'), ('MXN', r'MXN|MX\$|墨西哥比索|比索')):
        amounts = set()
        for match in re.finditer(r'(?:(?:' + pattern + r')\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:' + pattern + r'))', text, re.I):
            raw = match[1] or match[2]
            if re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', raw):
                number = float(raw.replace(',', ''))
                if math.isfinite(number):
                    amounts.add(number)
        result[currency] = next(iter(amounts)) if len(amounts) == 1 else None
    return result


def profit_reference(payload, captured_at, root):
    """Reference only: never feed these calculator costs/results into pricing facts."""
    from .autopricing_v3 import policy, margin_policy
    incoming = payload.get('zying_profit_reference') or {}
    if not isinstance(incoming, dict):
        raise ValueError('利润参考必须是对象')

    def number(raw):
        try:
            value = Decimal(raw)
            return value if value.is_finite() else None
        except (InvalidOperation, ValueError):
            return None

    def item(raw, currency=None, unit=None, origin='zying_calculator_prefill'):
        raw = str(raw or '').strip()[:200]
        numeric = raw.replace(',', '').rstrip('%').strip()
        value = number(numeric) if re.fullmatch(r'-?\d+(?:\.\d+)?', numeric) else None
        return {'raw': raw, 'value': float(value) if value is not None else None,
                'currency': currency, 'unit': unit, 'field_origin': origin, 'captured_at': captured_at}

    inputs = {}
    for key, currency, unit in (('purchase_cost_cny', 'CNY', None), ('shipping_cost_cny', 'CNY', None),
                                 ('converted_price_cny', 'CNY', None), ('commission_pct', None, '%'),
                                 ('selling_price_mxn', 'MXN', None)):
        source = (incoming.get('inputs') or {}).get(key) or {}
        inputs[key] = item(source.get('raw'), currency, unit)
    results = incoming.get('results') or {}
    margin = item((results.get('margin_pct') or {}).get('raw'), unit='%', origin='zying_calculator_result')
    raw_profit = str((results.get('net_profit') or {}).get('raw') or '').strip()[:200]
    profits = []
    currency_pattern = r'CNY|RMB|USD|MXN|US\$|MX\$|¥|￥|元|美元|比索'
    pattern = rf'({currency_pattern})\s*(-?[\d,]+(?:\.\d+)?)|(-?[\d,]+(?:\.\d+)?)\s*({currency_pattern})'
    currencies = {'RMB': 'CNY', '¥': 'CNY', '￥': 'CNY', '元': 'CNY', 'US$': 'USD', '美元': 'USD', 'MX$': 'MXN', '比索': 'MXN'}
    for match in re.finditer(pattern, raw_profit, re.I):
        code = (match[1] or match[4]).upper()
        currency = currencies.get(code, code)
        raw = match[2] or match[3]
        profits.append({**item(raw, currency, origin='zying_calculator_result'), 'raw': match[0]})
    if not profits:
        profits = [item(raw_profit, origin='zying_calculator_result')]
    trial = margin['value'] is not None or any(p['value'] is not None for p in profits)
    ratio = Decimal(str(margin['value'])) if margin['value'] is not None else None
    ratio_source = 'displayed_margin_pct' if ratio is not None else None
    if ratio is None:
        selling = inputs['selling_price_mxn']['value']
        amounts = {p['value'] for p in profits if p['currency'] == 'MXN' and p['value'] is not None}
        if selling is not None and selling > 0 and len(amounts) == 1:
            ratio = Decimal(str(next(iter(amounts)))) / Decimal(str(selling)) * 100
            ratio_source = 'net_profit_mxn_divided_by_calculator_buyer_price_mxn'
    decision = margin_policy(policy(root), 'BUYER_PRICE')
    hint = 'PENDING'
    if trial and ratio is not None and decision['status'] == 'CONFIRMED' and decision['minimum_margin_operator'] == 'GT':
        hint = 'PASS' if ratio > Decimal(decision['minimum_margin_pct']) else 'BELOW_FLOOR'
    return {'inputs': inputs, 'net_profit': profits, 'net_profit_raw': raw_profit,
            'margin_pct': margin, 'trial_calculated': trial, 'margin_hint': hint,
            'comparison_margin_pct': float(ratio) if ratio is not None else None,
            'comparison_source': ratio_source, 'margin_policy': decision,
            'policy_source': 'config/autopricing_policy.json; src/market_follow_v3.py:solve',
            'basis_verified': False, 'reference_only': True, 'captured_at': captured_at,
            'basis_note': '智赢净利润与项目贡献利润口径一致性未核；仅作参考，不参与定价事实'}


def normalize(payload, root=ROOT):
    if not isinstance(payload, dict) or _contains_forbidden_key(payload):
        raise ValueError('采集结果格式无效或包含禁止保存的字段')
    sid = str(payload.get('sku_id') or '').strip().upper()
    if not re.fullmatch(r'MLM\d{6,20}', sid) or payload.get('site_id', 'MLM') != 'MLM':
        raise ValueError('仅支持墨西哥 MLM SKU ID；其他站点拒绝采集')
    source_url = public_url(payload.get('source_url'), detail=True)
    observed = datetime.fromisoformat(str(payload.get('captured_at') or '').replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('采集时间必须含时区')
    fields = payload.get('fields')
    if not isinstance(fields, dict):
        raise ValueError('缺少已标注详情字段')
    raw = {k: str(fields.get(k) or '').strip()[:300 if k == 'title' else 600] for k in LABELS}
    origins = {k: (payload.get('field_origin', {}).get(k) if payload.get('field_origin', {}).get(k) in ('zying_calculator_prefill','zying_list_cache') else 'text')
               if raw[k] and raw[k] != '-' else None for k in LABELS}
    reference = {}
    for key in ('purchase_cost_cny', 'shipping_cost_cny', 'converted_price_cny'):
        item = payload.get('reference', {}).get(key)
        if isinstance(item, dict):
            reference[key] = {'value': str(item.get('value') or '')[:80], 'field_origin': 'zying_calculator_prefill'}
    price_observations = []
    for item in payload.get('price_observations', [])[:4]:
        if isinstance(item, dict):
            price_observations.append({k: str(item.get(k) or '')[:80] for k in ('value', 'label', 'field_origin')})
    dims = measurement(raw['dimensions'], True)
    weight = measurement(raw['weight'])
    volume = measurement(raw['volumetric_weight'])
    provenance = {'source': 'zying_detail', 'sku_id': sid, 'source_url': source_url,
                  'captured_at': observed.isoformat(), 'raw': raw, 'field_origin': origins,
                  'policy': 'ZYING_DETAIL_PRODUCT_AS_PACKAGE_MLM_V1'}
    package = dict(zip(FIELDS[:3], dims or [None] * 3))
    package.update(weight_g=weight[0] if weight else None, source='zying_detail',
                   status='ZYING_DETAIL_POLICY', evidence=copy.deepcopy(provenance))
    issues = []
    for field in FIELDS:
        if package[field] is None:
            issues.append({'sku_id': sid, 'code': 'ZYING_DETAIL_MISSING_PACKAGE', 'field': field,
                           'reason': '智赢详情缺少明确数值及单位：' + field})
    unshippable = bool(weight and weight[0] > 15000)
    if unshippable:
        issues.append({'sku_id': sid, 'code': 'ZYING_DETAIL_OVER_15KG', 'field': 'weight_g',
                       'reason': '不可发：单件重量超过 15000g（15kg）'})
    commission = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%', raw['commission'])
    sales = raw['sales']
    valid_sales = bool(re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)\s*(?:件|单|units?)?', sales, re.I))
    sales_number = int(re.sub(r'\D', '', sales)) if valid_sales else None
    image = public_url(str(payload['main_image'])[:1000]) if payload.get('main_image') else None
    origins['main_image'] = 'text' if image else None
    profit = profit_reference(payload, observed.isoformat(), root)
    requests = input_requests([], issues)
    if profit['margin_hint'] == 'PENDING':
        requests.append({'code': 'ZYING_PROFIT_REFERENCE_PENDING', 'field': 'zying_profit_reference',
                         'request': '请在智赢页填采购成本并试算后重新采集', 'sku_ids': [sid], 'blocking': False})
    for code,field,reason in (
        ('CATEGORY_MAPPING_PENDING','category_id','官方末级类目待核验；候选路径：'+(raw['category_path'] or '未选择/未缓存列表子类目')),
        ('ZYING_NET_PROCEEDS_PENDING','net_proceeds','净收益未试算/待补'),
        ('COMPETITOR_USD_PENDING','competitor_price','CBT竞品售价待补：页面未显示USD，不自行换汇')):
        if field=='net_proceeds' and any(v is not None for v in money(raw['net_proceeds']).values()):continue
        if field=='competitor_price' and money(raw['price'])['USD'] is not None:continue
        requests.append({'code':code,'field':field,'request':reason,'sku_ids':[sid],'blocking':False})
    cached=payload.get('list_cache') or {}
    cache_time=cached.get('cached_at') if cached.get('sku_id')==sid else None
    if cache_time:
        try:
            age=(observed-datetime.fromisoformat(str(cache_time).replace('Z','+00:00'))).total_seconds()
            if age<0 or age>7*86400:cache_time=None
        except (TypeError,ValueError):cache_time=None
    # An expired cache must never become a fresh text observation.
    for key in LABELS:
        if origins[key]=='zying_list_cache' and not cache_time:
            raise ValueError('列表缓存已过期或缺少时间，请重新缓存当前列表')
    if payload.get('field_origin',{}).get('price_usd')=='zying_list_cache':origins['price_usd']='zying_list_cache'
    buyer=money(raw['price'])
    if payload.get('field_origin',{}).get('price_usd')=='zying_list_cache' and not cache_time:
        raise ValueError('USD列表缓存已过期或缺少时间')
    competitor={'source':'competitor_price','reference_only':True,'note':'竞品参考价，不是最终售价',
                'amount':buyer['USD'],'currency_id':'USD','buyer_price_mxn':buyer['MXN'],
                'field_origin':payload.get('field_origin',{}).get('price_usd') or origins['price'],
                'captured_at':observed.isoformat(),'cached_at':cache_time,
                'final_price_status':'PENDING_CONTRIBUTION_GT_10_PERCENT'}
    return {**provenance, 'site_id': 'MLM', 'competitor_price':competitor,
            'list_cache':{'cached_at':cache_time,'sku_id':sid} if cache_time else {},
            'category_mapping':{'status':'CATEGORY_MAPPING_PENDING','candidate_path':raw['category_path'],'reason':'需由主人在编辑页触发官方类目匹配'}, 'package': package,
            'buyer_price': money(raw['price']), 'net_proceeds': money(raw['net_proceeds']),
            **{k: raw[k] or None for k in ('seller', 'fulfillment', 'category_path', 'listed_at', 'title', 'brand')},
            'volumetric_weight_g': volume[0] if volume else None,
            'commission_pct': float(commission[1]) if commission and float(commission[1]) <= 100 else None,
            'sales': sales_number, 'sales_range': sales if re.fullmatch(r'\d+\s*[-–]\s*\d+', sales) else None,
            'reference': reference, 'price_observations': price_observations, 'main_image': image,
            'zying_profit_reference': profit,
            'status': 'BLOCKED_EVIDENCE' if issues else 'CAPTURE_READY',
            'unshippable': unshippable, 'blocking_reasons': issues,
            'input_requests': requests,
            'selector_validation': 'UNVERIFIED_REAL_DOM',
            **{k: copy.deepcopy(payload.get(k)) for k in ('extension_version','background_extension_version','parser_version','payload_version','diagnostics','payload_archive') if k in payload}}


def build_draft(detail):
    pack = copy.deepcopy(detail['package'])
    provenance = {k: copy.deepcopy(detail[k]) for k in ('source', 'sku_id', 'source_url', 'captured_at', 'raw', 'policy', 'field_origin')}
    weight = ({'value': pack['weight_g'], 'unit': 'g', 'status': pack['status'], **provenance}
              if pack['weight_g'] is not None else None)
    dimensions = ({**dict(zip(('length', 'width', 'height'), (pack[k] for k in FIELDS[:3]))),
                   'unit': 'cm', 'status': pack['status'], **provenance}
                  if all(pack[k] is not None for k in FIELDS[:3]) else None)
    if weight:
        weight['field_origin'] = detail['field_origin']['weight']
    if dimensions:
        dimensions['field_origin'] = detail['field_origin']['dimensions']
    sid = detail['sku_id']
    gallery = ([{'remote_url': detail['main_image'], 'role': 'main', 'source': 'zying_detail',
                 'review_status': 'UNREVIEWED'}] if detail['main_image'] else [])
    return {'schema_version': '0.3', 'status': 'DRAFT_LOCAL_ONLY', 'human_approved': False,
            'queue_status': detail['status'], 'requested_action': 'prepare',
            'blocking_reasons': detail['blocking_reasons'], 'input_requests': detail['input_requests'],
            'payload': {'title': detail['title'] or sid, 'family_name': detail['title'] or sid,
                        'description': '', 'category_id': '', 'currency_id':'USD',
                        'competitor_price':copy.deepcopy(detail.get('competitor_price')),  'attributes': [], 'pictures': [],
                        'sites_to_sell': [], 'catalog_listing': False},
            'evidence': {'source_kind': 'zying_detail', 'source_product_id': sid,
                         'supplier_url': detail['source_url'], 'zying_detail': detail,
                         'competitor_price':copy.deepcopy(detail.get('competitor_price')),
                         'category_mapping':copy.deepcopy(detail.get('category_mapping')),
                         'zying_profit_reference': copy.deepcopy(detail['zying_profit_reference']),
                         'packed_weight': weight, 'packed_dimensions': dimensions,
                         'current_pricing_sku_id': sid,
                         'sku_details': [{'sku_id': sid, 'variant': '智赢详情参考商品', 'package': pack}],
                         'image_review': {'source_gallery': gallery, 'gallery': []},
                         'images_human_reviewed': False},
            'edit_metadata': {'source': 'zying_detail', 'field_locks': {},
                              'created_at': datetime.now(timezone.utc).isoformat()}}


def sanitized_payload(value, depth=0):
    """Bounded public archive. Reject excessive structures; strip sensitive keys/URLs."""
    if depth > 12:
        raise ValueError('采集数据嵌套过深')
    if isinstance(value, dict):
        if len(value) > 100: raise ValueError('采集字段过多')
        return {str(k): sanitized_payload(v, depth+1) for k,v in value.items()
                if not re.search(r'cookie|authorization|token|password|secret|api.?key|localstorage|script',str(k),re.I)}
    if isinstance(value, list):
        if len(value)>100: raise ValueError('采集数组过长')
        return [sanitized_payload(v,depth+1) for v in value]
    if isinstance(value,str):
        value=re.sub(r'<script\b[^>]*>.*?</script\s*>','[removed]',value,flags=re.I|re.S)
        value=re.sub(r'(?i)(cookie|authorization|token|password|api[_-]?key)\s*[:=]\s*[^\s;,]+',r'\1=[redacted]',value)
        value=re.sub(r'https?://[^\s<>"\']+',lambda m: public_url(m[0]),value)
        return value[:4000]
    if value is None or isinstance(value,(bool,int,float)): return value
    raise ValueError('采集值类型无效')


def merge_nonempty(old, new):
    result=copy.deepcopy(old)
    for k,v in new.items():
        if isinstance(v,dict) and isinstance(result.get(k),dict): result[k]=merge_nonempty(result[k],v)
        elif v is not None and v != '' and v != [] and v != '-' and v != '—': result[k]=copy.deepcopy(v)
    return result


def merge_draft(original, detail):
    """Capture owns only its evidence; title/package/gallery edits are compared to prior capture."""
    result=copy.deepcopy(original); ev=result['evidence']
    previous=ev['zying_detail']
    if 'field_origin' not in previous or 'zying_profit_reference' not in previous:
        base=normalize({'sku_id':previous['sku_id'],'site_id':'MLM','source_url':previous['source_url'],
            'captured_at':previous['captured_at'],'fields':previous['raw'],'main_image':previous.get('main_image')})
        previous={**base,**previous}
    old=build_draft(previous)
    merged=merge_nonempty(previous,detail)
    if not detail.get('zying_profit_reference',{}).get('trial_calculated') and previous.get('zying_profit_reference',{}).get('trial_calculated'):
        merged['zying_profit_reference']=copy.deepcopy(previous['zying_profit_reference'])
    # Re-normalize retained raw measurements so old nonempty facts remain coherent.
    raw=merged['raw']; normalized=normalize({'sku_id':merged['sku_id'],'site_id':'MLM',
        'source_url':merged['source_url'],'captured_at':merged['captured_at'],'fields':raw,
        'main_image':merged.get('main_image'),'field_origin':merged.get('field_origin',{}),
        'reference':merged.get('reference',{}),'price_observations':merged.get('price_observations',[]),
        'list_cache':merged.get('list_cache',{})})
    for k in ('package','blocking_reasons','status','unshippable','input_requests','buyer_price','commission_pct','competitor_price','category_mapping'):
        merged[k]=normalized[k]
    fresh=build_draft(merged)
    locks=(result.get('edit_metadata') or {}).get('field_locks') or {}
    pack_keys=('package','packed_weight','packed_dimensions','weight','dimensions',*FIELDS)
    # Compare with prior captured defaults: edited fields, locks and non-owned evidence survive.
    for k in ('title','family_name'):
        if result['payload'].get(k)==old['payload'].get(k) and not locks.get(k):
            result['payload'][k]=fresh['payload'][k]
    for k in ('packed_weight','packed_dimensions'):
        protected=('package',k,'weight','weight_g') if k=='packed_weight' else ('package',k,'dimensions',*FIELDS[:3])
        if ev.get(k)==old['evidence'].get(k) and not any(locks.get(f) is True for f in protected): ev[k]=fresh['evidence'][k]
    sid=detail['sku_id']
    for sku in ev.get('sku_details',[]):
        own=sku.get('human_field_locks') or {}
        if (sku.get('sku_id')==sid and sku.get('package')==old['evidence']['sku_details'][0]['package']
                and not any(locks.get(f) is True or own.get(f) is True for f in pack_keys)):
            sku['package']=fresh['evidence']['sku_details'][0]['package']
    image=ev.setdefault('image_review',{})
    source=image.setdefault('source_gallery',[])
    for row in fresh['evidence']['image_review']['source_gallery']:
        if not any(x.get('remote_url')==row['remote_url'] for x in source): source.append(row)
    ev['competitor_price']=copy.deepcopy(merged.get('competitor_price'))
    ev['category_mapping']=copy.deepcopy(merged.get('category_mapping'))
    if not locks.get('competitor_price'):result['payload']['competitor_price']=copy.deepcopy(merged.get('competitor_price'))
    ev['zying_detail']=merged; ev['zying_profit_reference']=copy.deepcopy(merged['zying_profit_reference'])
    prior_requests=previous.get('input_requests') or []
    result['input_requests']=[r for r in result.get('input_requests',[]) if r not in prior_requests]+merged['input_requests']
    prior_issues=previous.get('blocking_reasons') or []
    result['blocking_reasons']=[r for r in result.get('blocking_reasons',[]) if r not in prior_issues]+merged['blocking_reasons']
    result['queue_status']=merged['status']
    if result['payload'].get('category_id'):
        result['input_requests']=[r for r in result.get('input_requests',[]) if r.get('code')!='CATEGORY_MAPPING_PENDING']
    result['human_approved']=False
    if isinstance(result.get('pricing_plan'),dict): result['pricing_plan']['status']='STALE'
    return result


def collapse_rows(rows):
    """List-only deduplication; physical history stays untouched and discoverable."""
    out=[]; groups={}
    for row in rows:
        sid=row.get('zying_item_id')
        if not sid: out.append(row);continue
        groups.setdefault(sid,[]).append(row)
    for group in groups.values():
        group.sort(key=lambda r:(r.get('zying_captured_at') or '',r['name']),reverse=True)
        chosen=group[0]; chosen['duplicate_drafts']=[r['name'] for r in group[1:]]
        out.append(chosen)
    return out


def refresh_contribution(root, draft):
    from .pricing_result_v3 import contribution
    result=contribution(root,draft)
    draft['evidence']['project_contribution']=result
    draft['input_requests']=[r for r in draft.get('input_requests',[]) if r.get('origin')!='CONTRIBUTION_INPUT']+result['input_requests']
    return draft


def save(root, payload):
    """One MLM capture per item, versioned merge, plus one bounded public archive per call."""
    root=Path(root)
    if not isinstance(payload,dict) or len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())>32768:
        raise ValueError('采集结果格式/大小无效')
    clean=sanitized_payload(payload)
    detail=normalize(clean,root)
    for key in ('extension_version','background_extension_version','parser_version','payload_version'):
        detail[key]=str(clean.get(key) or 'UNKNOWN_LEGACY')[:80]
    # Archive safe bounded diagnostics only. Never retain arbitrary script/body content.
    diagnostic=clean.get('diagnostics') or {}
    safe_diag={'fields':{k:{'hit':v.get('hit') is True,'reason':str(v.get('reason',''))[:200]}
                 for k,v in (diagnostic.get('fields') or {}).items() if k in (*LABELS,'main_image','profit') and isinstance(v,dict)},
               'snippets':[{'kind':str(v.get('kind',''))[:30],'text':str(v.get('text',''))[:200]}
                    for v in diagnostic.get('snippets',[])[:40] if isinstance(v,dict)],
               'attempts':min(10,max(0,int(diagnostic.get('attempts') or 0)))}
    safe_diag['snippets']=list({(s['kind'],s['text']):s for s in safe_diag['snippets']}.values())
    clean['diagnostics']=safe_diag
    detail['diagnostics']=safe_diag
    archive_text=json.dumps(clean,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if len(archive_text.encode('utf-8'))>32768:
        raise ValueError('去敏存档超过32KB，本次未保存')
    with SAVE_LOCK:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]
        archive=Path('data/v3/zying_detail_payloads')/detail['sku_id']/(stamp+'.json')
        target=root/archive;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8') as stream: stream.write(archive_text)
        detail['payload_archive']=archive.as_posix()
        candidates=[]
        for path in (root/'drafts').glob('*-draft.json'):
            if path.is_symlink(): continue
            value=json.loads(path.read_text(encoding='utf-8'))
            ev=value.get('evidence') or {}
            if ev.get('source_kind')=='zying_detail' and ev.get('source_product_id')==detail['sku_id']:
                candidates.append(((ev.get('zying_detail') or {}).get('captured_at',''),path.name))
        if candidates:
            name=max(candidates)[1]
            from .draft_editor import save_edited_draft
            save_edited_draft(name,{},root/'drafts',root/'draft_versions',zying_capture=detail)
            mode='MERGED_PRESERVE_EDITS'
        else:
            name='zying-'+detail['sku_id']+'-'+uuid.uuid4().hex+'-draft.json'
            path=root/'drafts'/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as stream: json.dump(refresh_contribution(root,build_draft(detail)),stream,ensure_ascii=False,indent=2)
            mode='CREATED'
        persisted=json.loads((root/'drafts'/name).read_text(encoding='utf-8'))
        actual=persisted['evidence']['zying_detail']
    return {'ok':True,'name':name,'status':actual['status'],'input_requests':persisted['input_requests'],
            'unshippable':actual['unshippable'],'payload_archive':archive.as_posix(),
            'recapture_mode':mode,'enqueued':False,'publishing_performed':False}


def save_dom_snapshot(root, payload):
    """User-triggered diagnostic archive only; never touches drafts."""
    if not isinstance(payload,dict):raise ValueError('无效诊断对象')
    sid=str(payload.get('sku_id') or '')
    if not re.fullmatch(r'MLM\d{6,20}',sid) or payload.get('site_id')!='MLM':raise ValueError('仅支持MLM诊断')
    url=public_url(payload.get('source_url'),detail=True)
    observed=datetime.fromisoformat(str(payload.get('captured_at') or '').replace('Z','+00:00'))
    if observed.tzinfo is None:raise ValueError('诊断时间须有时区')
    rows=[]
    labels={'商品尺寸','产品尺寸','尺寸','商品重量(g)','商品重量','产品重量','重量','计泡重','品牌','brand','类目','子类目','类目路径','产品标题','商品标题','售价(当地货币)','售价(MXN)','平台佣金比','运送费(¥)','运送费(￥)','采购成本(¥)','采购成本(￥)','净利润','利润率','净收益','净收益价'}
    incoming=payload.get('snapshot')
    if not isinstance(incoming,list) or len(incoming)>24:raise ValueError('诊断行超限')
    for row in incoming:
        if not isinstance(row,dict) or row.get('label') not in labels:continue
        nodes=[]
        for node in (row.get('nodes') or [])[:10]:
            if not isinstance(node,dict) or node.get('tagName') not in ('LABEL','SPAN','INPUT','SELECT','DIV','P','DT'):continue
            nodes.append({k:str(node.get(k) or '')[:120 if k=='className' else 100] for k in ('tagName','className','text','value')})
        rows.append({'label':row['label'],'nodes':nodes})
    clean=sanitized_payload({'sku_id':sid,'site_id':'MLM','source_url':url,'captured_at':observed.isoformat(),
                             'background_extension_version':str(payload.get('background_extension_version') or '')[:80],'snapshot':rows})
    text=json.dumps(clean,ensure_ascii=False,allow_nan=False)
    if len(text.encode())>24000:raise ValueError('诊断超过24KB，请缩小区域')
    rel=Path('data/v3/zying_detail_payloads')/sid/('dom_snapshot_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    path=Path(root)/rel;path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:f.write(text)
    return {'ok':True,'payload_archive':rel.as_posix(),'draft_modified':False}
