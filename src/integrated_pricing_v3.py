"""Pricing is an AI processing stage; no platform price write or publication."""
import json,hashlib,copy,uuid
from pathlib import Path
from . import autopricing_v3 as pricing
from . import store_management as stores

def signature(draft):
    from decimal import Decimal
    def sites(rows):
        return sorted([{'site_id':r.get('site_id'),'price':r.get('price'),'net_proceeds':r.get('net_proceeds'),
            'shipping_cost_usd':r.get('shipping_cost_usd'),'logistic_type':r.get('logistic_type') or 'remote',
            'listing_type_id':r.get('listing_type_id') or 'gold_special'} for r in rows or []],key=lambda r:str(r['site_id']))
    def canonical(value):
        if isinstance(value,bool) or value is None:return value
        if isinstance(value,(float,int)):return str(Decimal(str(value)).normalize())
        if isinstance(value,dict):return {k:canonical(v) for k,v in value.items()}
        if isinstance(value,list):return [canonical(v) for v in value]
        return value
    ev=draft.get('evidence') or {};skus=[]
    for row in ev.get('sku_details',[]):
        item={k:row.get(k) for k in ('sku_id','purchase_cost_cny','domestic_shipping_cny','package','target_net_proceeds_usd','market_reference','procurement')}
        item['site_pricing']=sites(row.get('site_pricing'));skus.append(item)
    plan={k:v for k,v in (draft.get('pricing_plan') or {}).items() if k not in ('profit_guidance','status','note')}
    from .procurement_v3 import source_signature
    source=[source_signature(draft,s) for s in (ev.get('supplier_page') or {}).get('skus') or []] if any('procurement' in s for s in ev.get('sku_details') or []) else None
    data={'pricing_policy_version':pricing.VERSION,'skus':skus,'packed_weight':ev.get('packed_weight'),'packed_dimensions':ev.get('packed_dimensions'),'sites':sites((draft.get('payload') or {}).get('sites_to_sell')),'pricing':plan,'market_reference':ev.get('market_reference'),'procurement_source_signatures':source}
    return hashlib.sha256(json.dumps(canonical(data),sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()

def summary(draft,root=None):
    data=(draft.get('evidence') or {}).get('ai_processing_pricing')
    if not data:return {'state':'NOT_RUN','rows':[],'errors':['尚未运行集成利润定价；历史商品需重新处理或在利润与比价页核对。']}
    if data.get('state') in ('BUDGET_ESTIMATE','PARTIAL_BUDGET_ESTIMATE','MARGIN_POLICY_PENDING','MANUAL_PRICE_LOCKED') and (data.get('input_signature')!=signature(draft) or data.get('policy_fingerprint')!=pricing.policy_fingerprint(root or Path(__file__).resolve().parents[1])):
        return {**data,'state':'STALE','errors':['SKU、成本、价格或包装已变更，请重新运行利润定价。']}
    return {k:data.get(k) for k in ('state','store_id','site_id','query','plan_id','rows','errors','warnings','reference_issues','requires_budget_review','limitations','method','margin_policy','locked_sku_ids')}

def run(path,root):
    path=Path(path);root=Path(root)
    draft=json.loads(path.read_text(encoding='utf-8'))
    evidence=draft.get('evidence') or {}
    state={'state':'BLOCKED','rows':[],'errors':[],'limitations':'预算贡献利润，不是实得净利润；净回款不是买家售价，竞品仅供参考。'}
    input_hash=None
    try:
        store_id=str(stores.store_status(root).get('active_store_id') or '')
        if not store_id:raise ValueError('请先在店铺管理选择当前店铺')
        sites={str(s.get('site_id') or '') for s in (draft.get('payload') or {}).get('sites_to_sell',[])}
        if sites!={'MLM'}:raise ValueError('集成定价当前仅支持墨西哥MLM；请明确目标站点，其他站点需单独核价')
        query=str(evidence.get('pricing_query') or (draft.get('payload') or {}).get('title') or '').strip()
        if not query:raise ValueError('缺少目标站点商品标题或明确比价词')
        state.update(store_id=store_id,site_id='MLM',query=query)
        plan=pricing.prepare(root,{'name':path.name,'store_id':store_id,'site_id':'MLM','query':query,'allow_cost_only':True})
        input_hash=plan.get('draft_hash');state['policy_fingerprint']=plan.get('policy_hash')
        state.update(plan_id=plan['id'],rows=plan.get('rows',[]),errors=plan.get('errors',[]),warnings=plan.get('warnings',[]),reference_issues=plan.get('reference_issues',[]),requires_budget_review=True,method=plan.get('method'),margin_policy=plan.get('margin_policy'),locked_sku_ids=plan.get('locked_sku_ids') or [],limitations=plan.get('limitations') or state['limitations'])
        if plan['state']=='MARGIN_POLICY_PENDING':
            state['state']='MARGIN_POLICY_PENDING'
            reason='净回款预算尚不能核对已确认的买家售价利润底线' if (plan.get('margin_policy') or {}).get('reason')=='BASIS_NOT_COMPARABLE' else '利润底线及计算口径待用户确认'
            raise ValueError('费用与利润候选已保存；'+reason+'，未改站点价格')
        if plan.get('locked_sku_ids'):
            state['state']='MANUAL_PRICE_LOCKED'
            state['errors'].append('SKU '+', '.join(plan['locked_sku_ids'])+'：价格已人工锁定；候选保留，原价不变')
            plan={**plan,'state':'BLOCKED','errors':state['errors']}
        if plan['state']!='READY_ESTIMATED' and plan.get('rows'):
            passing=[r for r in plan['rows'] if str(r['sku_id']) not in set(plan.get('locked_sku_ids') or []) and (r.get('floor_passed') is True if plan.get('method')=='FOLLOW_REMOTE_MARKET_PRICE' else pricing.prices.number(r.get('margin_pct'),'预算利润率')>=pricing.prices.number(plan['policy']['minimum_margin_pct'],'预算下限'))]
            if passing:
                partial=copy.deepcopy(plan);partial.update(id=uuid.uuid4().hex,parent_plan_id=plan['id'],rows=passing,errors=[],state='READY_ESTIMATED')
                partial['warnings'].append('仅将通过成本与底线校验的SKU写入本机，其他SKU保持原值、继续待补。')
                pricing.save(root/'data/v3/pricing/plans'/(partial['id']+'.json'),partial)
                pricing.apply(root,partial['id'],True)
                state.update(state='PARTIAL_BUDGET_ESTIMATE',applied_plan_id=partial['id'],applied_sku_ids=[r['sku_id'] for r in passing])
        if plan['state']!='READY_ESTIMATED':raise ValueError('；'.join(state['errors']) or '定价依据不足')
        pricing.apply(root,plan['id'],True) # Explicit full-flow invocation authorizes reversible local draft pricing.
        state.update(state='BUDGET_ESTIMATE',errors=[])
    except Exception as exc:
        if not state['errors']:state['errors']=[str(exc)[:500]]
    current_bytes=path.read_bytes();draft=json.loads(current_bytes)
    if state['state'] in ('MARGIN_POLICY_PENDING','MANUAL_PRICE_LOCKED') and input_hash!=hashlib.sha256(current_bytes).hexdigest():
        state.update(state='STALE',errors=['计算期间商品已修改；候选保留于本机计划，未将旧输入结果当新结果'])
    if state['state'] in ('BUDGET_ESTIMATE','PARTIAL_BUDGET_ESTIMATE','MARGIN_POLICY_PENDING','MANUAL_PRICE_LOCKED'):
        state['input_signature']=signature(draft)
        if not state.get('policy_fingerprint'):state['policy_fingerprint']=pricing.policy_fingerprint(root)
    draft.setdefault('evidence',{})['ai_processing_pricing']=state
    pricing.save(path,draft)
    if state['state'] in ('BUDGET_ESTIMATE','PARTIAL_BUDGET_ESTIMATE'):
        applied_id=state.get('applied_plan_id') or state.get('plan_id')
        pp=root/'data/v3/pricing/plans'/(applied_id+'.json')
        owned=json.loads(pp.read_text('utf-8'))
        # Include this owner's status write in the undo guard; subsequent human
        # edits still change the hash and cannot be overwritten by undo.
        owned['applied_hash']=pricing.hashfile(path);pricing.save(pp,owned)
    if state['state'] in ('BLOCKED','STALE','PARTIAL_BUDGET_ESTIMATE','MARGIN_POLICY_PENDING','MANUAL_PRICE_LOCKED'):raise ValueError('利润与定价暂停：'+str(len(state['errors']))+' 项待补；'+state['errors'][0])
    return state

def install(legacy):
    if getattr(legacy,'_integrated_pricing_installed',False):return
    legacy._integrated_pricing_installed=True
    legacy.refresh_competitor_pricing=run
