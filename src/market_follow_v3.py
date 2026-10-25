"""Follow a selected remote offer; accept only the user's contribution floor."""
import copy, hashlib, json, re, time, uuid
from datetime import datetime, timezone
from decimal import Decimal
from . import pricing_tools_v3 as prices

VERSION = "MARKET_FOLLOW_20261019_GT_FLOOR_V2"

def reference_issue(draft,sku,ref):
    """A navigation reference is a lead, not proof of the same SKU or pack."""
    from .product_match_v3 import assess
    result=assess(draft,sku,ref)
    if result['issues']:return result['issues'][0]
    return None
def reference(raw):
    if not isinstance(raw,dict):return None
    item=str(raw.get('market_item_id') or '')
    if not re.fullmatch(r'MLM\d{6,20}',item):raise ValueError('竞品商品编号无效')
    if raw.get('site_id')!='MLM' or raw.get('fulfillment')!='remote':raise ValueError('只跟随墨西哥自发货竞品')
    gross=prices.number(raw.get('buyer_price_usd'),'竞品售价');net=prices.number(raw.get('net_proceeds_usd'),'竞品净收益价')
    if not gross or not net or net>gross:raise ValueError('竞品售价或净收益价无效')
    observed=datetime.fromisoformat(str(raw.get('observed_at') or '').replace('Z','+00:00'))
    if observed.tzinfo is None:raise ValueError('竞品采集时间缺时区')
    age=(datetime.now(timezone.utc)-observed).total_seconds()
    if not 0<=age<=86400:raise ValueError('竞品价格超过24小时，请重新读取')
    result={'market_item_id':item,'site_id':'MLM','fulfillment':'remote','buyer_price_usd':str(gross),'net_proceeds_usd':str(net),
            'observed_at':observed.isoformat(),'source_type':'ZYING_VISIBLE_TABLE',
            'source_url':'https://meli.zying.net/#/goods/total','category_path':str(raw.get('category_path') or '')[:600],
            'title':str(raw.get('title') or '')[:300],'spec_match':str(raw.get('spec_match') or 'SOURCE_SELECTED_PENDING_REVIEW')}
    for key in ('source_sku_id','supplier_offer_id','market_sku_id','units_per_pack','identity_evidence','required_identity_fields','resolved_item_id','source_capture_sha256'):
        if key in raw:result[key]=copy.deepcopy(raw[key])
    return result

def solve(gross,net,purchase,domestic,forwarder,packaging,fx,p):
    from .autopricing_v3 import pct,margin_policy
    gross=prices.number(gross,'跟随售价');net=prices.number(net,'净收益价');fx=prices.number(fx,'汇率')
    if not gross or not net or net>gross or not fx:raise ValueError('售价、净收益价或汇率无效')
    values=[prices.number(v,k) for v,k in zip((purchase,domestic,forwarder,packaging,p.get('other_cny')),('采购','国内运费','货代','包材','其他'))]
    if None in values:raise ValueError('卖家成本缺失，不能核算利润底线')
    base=sum(values,Decimal(0));reserve=base*pct(p,'returns_base_pct')
    ads_ratio=pct(p,'ads_net_pct');tax_ratio=pct(p,'tax_reserve_net_pct')
    if ads_ratio+tax_ratio>=1:raise ValueError('广告与税费预算比例合计需小于100%')
    ads=net*ads_ratio;tax=net*tax_ratio
    profit=net-(base+reserve)/fx-ads-tax
    margin=profit/gross*100;decision_policy=margin_policy(p,'BUYER_PRICE')
    floor=prices.number(decision_policy['minimum_margin_pct'],'利润率底线')
    passed=(margin>floor if p.get('minimum_margin_operator')=='GT' else margin>=floor) if floor is not None else None
    high=net-(base+reserve*2)/fx-ads*2-tax*2
    return {'buyer_price_usd':prices.money(gross),'net_proceeds_usd':prices.money(net),'base_cost_cny':prices.money(base),
      'returns_reserve_cny':prices.money(reserve),'ads_budget_usd':prices.money(ads),'tax_reserve_usd':prices.money(tax),
      'unit_contribution_usd':prices.money(profit),'sales_margin_pct':prices.money(margin),
      'margin_pct':prices.money(profit/net*100),'margin_basis':'BUYER_PRICE','minimum_margin_pct':prices.money(floor) if floor is not None else None,
      'margin_policy':decision_policy,'floor_passed':passed,'minimum_margin_operator':p.get('minimum_margin_operator','GTE'),'decision':'MARGIN_POLICY_PENDING' if floor is None else 'PASS_FLOOR' if passed else 'BELOW_FLOOR',
      'stress_margin_pct':prices.money(high/gross*100),'break_even_net_usd':prices.money((base+reserve)/fx/(1-ads_ratio-tax_ratio)),
      'purchase_cny':prices.money(values[0]),'domestic_cny':prices.money(values[1]),'forwarder_cny':prices.money(values[2]),'packaging_cny':prices.money(values[3]),
      'formula':'贡献利润=竞品净收益参考-本SKU卖家变动成本；销售收入贡献利润率=贡献利润/跟随售价；低于底线不抬价',
      'assumptions':['智赢净收益价是第三方回款参考，最终平台费用和到账需核对','广告、另付税费及退货预留采用已授权预算，不是实际经营数据',str(p.get('packaging_selection') or ''),'国内运费暂按一件一单分摊；固定开支未分摊']}

def prepare(root,value):
    from . import autopricing_v3 as pricing
    root=pricing.Path(root);path=pricing.draft_path(root,value.get('name'));raw=path.read_bytes();draft=json.loads(raw)
    store_id=str(value.get('store_id') or '');prices.operations.store(root,store_id)
    if str(value.get('site_id') or 'MLM')!='MLM':raise ValueError('跟价当前仅支持MLM')
    p=pricing.policy(root);base_hash=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    overrides=value.get('budget') or {}
    allowed={'minimum_margin_pct','ads_net_pct','tax_reserve_net_pct','returns_base_pct','packaging_material_cny','other_cny'}
    if not isinstance(overrides,dict) or set(overrides)-allowed:raise ValueError('跟价预算字段无效；不使用目标利润反推售价')
    p={**p,**overrides}
    decision_policy=pricing.margin_policy(p,'BUYER_PRICE')
    skus=(draft.get('evidence') or {}).get('sku_details') or [];sid=str(value.get('sku_id') or '')
    chosen=[s for s in skus if not sid or str(s.get('sku_id'))==sid]
    if not chosen:raise ValueError('未找到SKU')
    plan={'ok':True,'state':'BLOCKED','id':uuid.uuid4().hex,'name':path.name,'store_id':store_id,'site_id':'MLM',
      'created_at':prices.stamp(),'created_epoch':time.time(),'draft_hash':hashlib.sha256(raw).hexdigest(),'policy':p,'policy_hash':base_hash,
      'rows':[],'errors':[],'warnings':[],'reference_issues':[],'method':'FOLLOW_REMOTE_MARKET_PRICE','local_only':True,
      'comparison':{'source':'智赢自发货榜单','platform_queried_at':None,'items':[]},
      'limitations':'跟随智赢自发货参考价，贡献利润率按买家售价计算；净收益参考不等于实际到账；同规格、费用与利润在发布前核对，不承诺完全相同买家成交价。'}
    quote=None;plan['exchange_rate']={}
    for sku in chosen:
        try:
            try:
                ref=reference(sku.get('market_reference') or ((draft.get('evidence') or {}).get('market_reference') if len(skus)==1 else None))
            except ValueError as exc:
                plan['reference_issues'].append({'sku_id':str(sku.get('sku_id')),'code':'REFERENCE_STALE' if '24小时' in str(exc) else 'REFERENCE_UNAVAILABLE','field':'market_reference','reason':str(exc)})
                raise
            if not ref:raise ValueError('未绑定该SKU对应的智赢自发货价格与净收益价')
            issue=reference_issue(draft,sku,ref)
            if issue:
                plan['reference_issues'].append({'sku_id':str(sku['sku_id']),**issue})
                raise ValueError(issue['reason'])
            if quote is None:quote=prices.rate_quote({'currency':'USD'});plan['exchange_rate']=quote
            plan['comparison']['platform_queried_at']=ref['observed_at'];plan['comparison']['items'].append({**ref,'price':ref['buyer_price_usd'],'currency':'USD','permalink':'https://articulo.mercadolibre.com.mx/MLM-'+ref['market_item_id'][3:]+'-_JM'})
            sites=[s for s in sku.get('site_pricing',[]) if s.get('site_id')=='MLM']
            if len(sites)!=1 or sites[0].get('logistic_type')!='remote' or sites[0].get('price') is not None:raise ValueError('SKU需唯一Remote净回款站点配置')
            from .procurement_v3 import purchase_cost
            row=solve(ref['buyer_price_usd'],ref['net_proceeds_usd'],purchase_cost(draft,sku),sku.get('domestic_shipping_cny',(draft.get('pricing_plan') or {}).get('domestic_shipping_cny')),
                      pricing.forwarder(sku,draft,p),p.get('packaging_material_cny'),quote.get('cny_per_currency'),p)
            row.update(sku_id=str(sku['sku_id']),variant=sku.get('variant'),market_reference=ref,previous_net_proceeds_usd=sites[0].get('net_proceeds'))
            plan['rows'].append(row)
            if row['floor_passed'] is False:plan['errors'].append(str(sku['sku_id'])+'：跟价利润率 '+row['sales_margin_pct']+'%，'+('未超过 ' if p.get('minimum_margin_operator')=='GT' else '低于 ')+row['minimum_margin_pct']+'% 底线；不自动抬价')
        except ValueError as exc:plan['errors'].append(str(sku.get('sku_id'))+'：'+str(exc))
    plan['margin_policy']=decision_policy
    plan['locked_sku_ids']=[str(s['sku_id']) for s in chosen if pricing.price_locked(draft,s,'MLM')]
    if plan['rows'] and decision_policy['status']!='CONFIRMED':plan['state']='MARGIN_POLICY_PENDING'
    elif plan['rows'] and not plan['errors']:plan['state']='READY_ESTIMATED'
    pricing.save(root/'data/v3/pricing/plans'/(plan['id']+'.json'),plan)
    return plan
