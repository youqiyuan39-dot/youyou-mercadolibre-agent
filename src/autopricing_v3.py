"""Evidence-bounded cost target pricing. Only writes reversible local draft prices."""
from __future__ import annotations
import copy,hashlib,json,re,threading,time,uuid,unicodedata,statistics
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
from . import pricing_tools_v3 as prices
LOCK=threading.RLock()
VERSION="2026-10-18-margin-policy-and-sku-cost-4"
DEFAULT_POLICY={
 "version":VERSION,"target_margin_pct":"30","minimum_margin_pct":"10","margin_policy_status":"PENDING_CONFIRMATION",
 "ads_net_pct":"5","tax_reserve_net_pct":"5","returns_base_pct":"3","other_cny":"0",
 "scope":"单件单SKU、Remote净回款；预算由用户授权估计，可修改；不是实际税率、退款率或广告表现",
 "authorization":"2026-10-11 用户要求读取货代表；无实操项计算并估计",
 "forwarder_source":"docs/鑫标服务收费明细26.8.15_转换版.docx",
 "packing_service_cny":"0.6","packaging_material_cny":"0","packaging_selection":"原包装＋免费25×35cm快递袋；预算选择，实际打包须核对；不含额外纸箱/气泡膜"}
def save(path,data):
    from .atomic_json_v3 import write
    write(path,data)
def policy(root):
    path=Path(root)/"config/autopricing_policy.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else copy.deepcopy(DEFAULT_POLICY)
def policy_fingerprint(root):
    return hashlib.sha256(json.dumps(policy(root),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def margin_policy(p,basis):
    """A historical proposal is not permission to release a product."""
    confirmed=p.get('margin_policy_status')=='CONFIRMED' and p.get('confirmed_margin_basis')==basis
    floor=prices.number(p.get('minimum_margin_pct'),'最低利润率')
    if floor is None or floor>=100:raise ValueError('利润率提案需在0至100之间')
    return {'status':'CONFIRMED' if confirmed else 'PENDING_CONFIRMATION','basis':basis,'minimum_margin_operator':p.get('minimum_margin_operator','GTE'),
            'configured_status':p.get('margin_policy_status'),'confirmed_basis':p.get('confirmed_margin_basis'),
            'reason':None if confirmed else 'BASIS_NOT_COMPARABLE' if p.get('margin_policy_status')=='CONFIRMED' else 'USER_THRESHOLD_UNCONFIRMED',
            'proposed_minimum_margin_pct':prices.money(floor),'minimum_margin_pct':prices.money(floor) if confirmed else None}
def price_locked(draft,sku,site):
    keys={'pricing','price','net_proceeds','target_net_proceeds_usd','site_pricing'}
    locks=(draft.get('edit_metadata') or {}).get('field_locks') or {}
    own=sku.get('human_field_locks') or {}
    return any(locks.get(k) is True or own.get(k) is True for k in keys) or any(r.get('site_id')==site and (r.get('locked') is True or r.get('manual_locked') is True) for r in sku.get('site_pricing') or [])
def hashfile(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def draft_path(root,name):
    if not isinstance(name,str) or not re.fullmatch(r"[\w.-]{1,180}-draft\.json",name) or ".." in name:raise ValueError("商品文件名无效")
    path=Path(root)/"drafts"/name
    if not path.resolve().is_relative_to((Path(root)/"drafts").resolve()) or not path.is_file():raise ValueError("商品不存在或路径不正确")
    return path
def pct(p,key):
    n=prices.number(p.get(key),key)
    if n is None or n>=100:raise ValueError(key+"需在0至100之间")
    return n/100
def solve(purchase,domestic,forwarder,packaging,fx,p):
    target=pct(p,"target_margin_pct");floor=pct(p,"minimum_margin_pct")
    ads=pct(p,"ads_net_pct");tax=pct(p,"tax_reserve_net_pct");returns=pct(p,"returns_base_pct")
    if target<floor or target+ads+tax>=1:raise ValueError("目标利润率或费用预留比例不正确")
    fx=prices.number(fx,"参考汇率")
    if not fx:raise ValueError("汇率必须大于0")
    values=[prices.number(v,k) for v,k in zip((purchase,domestic,forwarder,packaging,p.get("other_cny")),("采购","国内运费","货代","包材","其他"))]
    if None in values:raise ValueError("基础成本缺失，不能自主定价")
    base=sum(values,Decimal(0))
    if base<=0:raise ValueError("基础成本必须大于0")
    reserve=base*returns;cost=(base+reserve)/fx
    net=(cost/(1-target-ads-tax)).quantize(Decimal(".01"),rounding=ROUND_CEILING)
    profit=net*(1-ads-tax)-cost
    # Sensitivity is a budget scenario, not an observed likelihood.
    high=net*(1-ads*2-tax*2)-(base+reserve*2)/fx
    return dict(margin_policy=margin_policy(p,'NET_PROCEEDS'),net_proceeds_usd=prices.money(net),base_cost_cny=prices.money(base),returns_reserve_cny=prices.money(reserve),
      ads_budget_usd=prices.money(net*ads),tax_reserve_usd=prices.money(net*tax),
      unit_contribution_usd=prices.money(profit),margin_pct=prices.money(profit/net*100),
      stress_margin_pct=prices.money(high/net*100),break_even_net_usd=prices.money(cost/(1-ads-tax)),
      purchase_cny=prices.money(values[0]),domestic_cny=prices.money(values[1]),forwarder_cny=prices.money(values[2]),packaging_cny=prices.money(values[3]),
      formula="净回款=(基础成本CNY×(1+退货成本预留比例)/汇率)/(1-目标净回款贡献利润率-广告预留比例-税费现金预留比例)",
      assumptions=["广告及税费现金预留按净回款各"+str(p["ads_net_pct"])+"%、"+str(p["tax_reserve_net_pct"])+"%；不代表平台实际扣款或法定税率",
       "退货损耗预留按基础成本"+str(p["returns_base_pct"])+"%，不是实际退款率；压力情景将以上三项预留翻倍",
       str(p["packaging_selection"]),"国内运费暂按一件一单全额分摊；固定开支未分摊"])
def package_values(sku,draft):
    if 'procurement' in sku:
        from .procurement_v3 import assess
        result=assess(draft,sku)
        if result['issues']:raise ValueError('采购组成待核对：'+result['issues'][0]['reason'])
    package=sku.get("package") or {};ev=draft.get("evidence") or {}
    # Common packaging belongs only to the selected/single SKU, never its siblings.
    same_sku=len(ev.get('sku_details') or [])<=1 or str(sku.get('sku_id'))==str(ev.get('current_pricing_sku_id'))
    dims=(ev.get("packed_dimensions") or {}) if same_sku else {}
    # Read selected SKU dimensions first, then explicitly recorded shared dimensions.
    attrs={a.get("id"):a.get("value_name") for a in (draft.get("payload") or {}).get("attributes",[]) if isinstance(a,dict)} if same_sku else {}
    values=[]
    for field,shared,attr in (("length_cm","length","PACKAGE_LENGTH"),("width_cm","width","PACKAGE_WIDTH"),("height_cm","height","PACKAGE_HEIGHT")):
        raw=package.get(field)
        if raw is None:raw=dims.get(shared)
        if raw is None:raw=attrs.get(attr)
        match=re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:cm)?",str(raw or ""),re.I)
        if not match:raise ValueError("缺少包装尺寸，无法核对货代计费")
        n=prices.number(match.group(1),"包装尺寸")
        if not n:raise ValueError("包装尺寸必须大于0")
        values.append(n)
    weight=prices.number(package.get("weight_g") if package.get('weight_g') is not None else ((ev.get("packed_weight") or {}).get("value") if same_sku else None),"包装重量")
    if not weight:raise ValueError("缺少包装重量，无法核对货代计费")
    if (package.get('field_status') or {}).get('weight_g')=='WEIGHT_TYPE_UNCONFIRMED':raise ValueError('已采到重量，但净重/含包装重类型未确认，不能用于货代计费')
    return values,weight

def forwarder(sku,draft,p):
    values,weight=package_values(sku,draft)
    billable=weight/1000
    if max(values)>45:billable=max(billable,values[0]*values[1]*values[2]/12000)
    extra=max(Decimal(0),billable-5)
    service=prices.number(p.get("packing_service_cny"),"基础打包费")
    if service is None:raise ValueError("缺少货代基础打包费")
    if 'procurement' in sku:
        from .procurement_v3 import assess
        service*=assess(draft,sku)['distinct_source_skus']
    return service+extra
def market_reference(draft,comparison):
    title=str((draft.get("payload") or {}).get("title") or "").lower()
    result={"state":"NEEDS_MANUAL_SPEC","items":[],"bands":[],"note":"未确认完全同款、包装规格与到手运费；相近特征不等于相同SKU。"}
    if not ("wrench" in title and "telescop" in title and re.search(r"\b10\b",title)):
        return result
    for item in comparison.get("items",[]):
        text=unicodedata.normalize("NFKD",str(item.get("title") or "").lower())
        text="".join(c for c in text if not unicodedata.combining(c))
        size=bool(re.search(r"\b10\s*(?:pulg|pole|inch|in\b)",text))
        telescopic="telescop" in text or "retractil" in text
        five=bool(re.search(r"\b5\s*(?:en|em|in)\s*1\b",text))
        if size and telescopic and five and item.get("price") and item.get("shipping_origin")=="international":
            result["items"].append({**item,"spec_match":"SIMILAR_FEATURES_NOT_SAME_SKU"})
    groups={}
    for item in result["items"]:groups.setdefault((item["currency"],item.get("condition"),item.get("free_shipping")),[]).append(Decimal(item["price"]))
    for (cur,condition,shipping),v in groups.items():
        result["bands"].append({"currency":cur,"condition":condition,"free_shipping":shipping,"min":prices.money(min(v)),"median":prices.money(statistics.median(v)),"max":prices.money(max(v)),"count":len(v)})
    if result["items"]:result["state"]="SIMILAR_FEATURES_ONLY"
    return result

def _market_assessment(root,draft,sku,row,reference,site,store_id):
    result={"state":"UNAVAILABLE","note":"未取得适用且未过期的官方费用测算，净回款不能直接比较买家售价。"}
    path=Path(root)/"data/v3/pricing/official-fee-reference.json"
    if not path.is_file():return result
    q=json.loads(path.read_text(encoding="utf-8"))
    if not 0<=time.time()-float(q["observed_epoch"])<=min(float(q.get("max_age_seconds",0)),21600):return {**result,"note":"官方费用参考已过期，停止换算；成本预算仍可单独使用。"}
    sites=[s for s in sku.get("site_pricing",[]) if s.get("site_id")==site]
    if len(sites)!=1:return result
    match=(q.get("store_id")==store_id and q.get("site_id")==site and
      q.get("category_id")==draft.get("payload",{}).get("category_id") and
      q.get("logistic_type")==sites[0].get("logistic_type") and
      q.get("listing_type_id")==sites[0].get("listing_type_id") and
      prices.number(sku.get("package",{}).get("weight_g"),"重量")==Decimal(str(q.get("weight_g"))))
    if not match:return {**result,"note":"店铺、类目、路线、刊登类型或包装重量与费用参考不同，停止换算。"}
    fee=prices.number(q["commission_pct"],"佣金")/100
    if not 0<=fee<1:raise ValueError("官方佣金参考无效")
    gross=((Decimal(row["net_proceeds_usd"])+Decimal(q["international_shipping_usd"])+Decimal(q["local_shipping_usd"]))/(1-fee)).quantize(Decimal(".01"),rounding=ROUND_CEILING)
    if not Decimal(q["price_low_usd"])<=gross<=Decimal(q["price_high_usd"]):return {**result,"note":"预算售价超出已验证的官方费用价格区间，停止外推。"}
    mxn=gross*Decimal(q["usd_mxn"]["rate"])
    bands=[b for b in reference.get("bands",[]) if b.get("currency")=="MXN"]
    position="NO_SCREENED_PEERS";gap=None
    if len(bands)==1:
        low,high=Decimal(bands[0]["min"]),Decimal(bands[0]["max"])
        position="ABOVE_OBSERVED_RANGE" if mxn>high else ("BELOW_OBSERVED_RANGE" if mxn<low else "WITHIN_OBSERVED_RANGE")
        if high>0:gap=prices.money((mxn/high-1)*100)
    return {"state":"ESTIMATED_BUYER_PRICE","gross_usd":prices.money(gross),"gross_mxn_reference":prices.money(mxn),
      "peer_position":position,"gap_to_peer_max_pct":gap,"fee_reference":q,"peer_bands":bands,
      "decision":"保留成本预算目标；仅有相近标题候选，未确认同款，不据此自动降价或承诺竞争力。",
      "note":"参考售价已包含本次测算的平台佣金与国际运费；这些费用不再从净回款重复扣。比索换算与竞品包邮提示都不是结账含税到手价。"}

def market_assessment(*args):
    try:return _market_assessment(*args)
    except (KeyError,TypeError,ValueError,OSError):return {"state":"UNAVAILABLE","note":"费用参考格式无效，本次未取得可用的买家售价换算。"}

def prepare(root,value):
    if policy(root).get('pricing_strategy')=='FOLLOW_MARKET':
        from .market_follow_v3 import prepare as follow
        plan=follow(root,value)
        unavailable=all('未绑定该SKU对应' in e for e in plan.get('errors') or []) or len(plan.get('reference_issues') or [])==len(plan.get('errors') or [])
        if value.get('allow_cost_only') is True and policy(root).get('fallback_on_missing_reference',True) and not plan.get('rows') and plan.get('errors') and unavailable:
            return prepare_cost(root,value,fallback_from=plan)
        return plan
    return prepare_cost(root,value)

def prepare_cost(root,value,fallback_from=None):
    root=Path(root);path=draft_path(root,value.get("name"));raw=path.read_bytes();draft=json.loads(raw.decode("utf-8"))
    store_id=str(value.get("store_id") or "");prices.operations.store(root,store_id)
    site=str(value.get("site_id") or "MLM").upper()
    p=policy(root);base_policy_hash=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    overrides=value.get("budget") or {}
    allowed={"target_margin_pct","minimum_margin_pct","ads_net_pct","tax_reserve_net_pct","returns_base_pct","packaging_material_cny","other_cny"}
    if not isinstance(overrides,dict) or set(overrides)-allowed:raise ValueError("预算字段无效")
    p={**p,**overrides}
    if fallback_from:
        # Preserve FOLLOW_MARKET as primary policy. This separate candidate does
        # not assert that a net-return margin meets the buyer-price margin floor.
        p['target_margin_pct']=p.get('cost_budget_margin_pct') or p['minimum_margin_pct']
    if "packaging_material_cny" in overrides and str(overrides["packaging_material_cny"]) not in {"0","0.0","0.00"}:p["packaging_selection"]="用户填写的包材预算；实际按货代选用材料结算"
    sku_id=str(value.get("sku_id") or "")
    skus=[s for s in (draft.get("evidence") or {}).get("sku_details",[]) if isinstance(s,dict) and (not sku_id or str(s.get("sku_id"))==sku_id)]
    if not skus:raise ValueError("未找到SKU")
    plan={"ok":True,"state":"BLOCKED","id":uuid.uuid4().hex,"name":path.name,"store_id":store_id,"site_id":site,
      "created_at":prices.stamp(),"created_epoch":time.time(),"draft_hash":hashlib.sha256(raw).hexdigest(),
      "policy":p,"policy_hash":base_policy_hash,
      "rows":[],"errors":[],"warnings":[],"method":"COST_TARGET_WITH_MARKET_REFERENCE","local_only":True,
      "limitations":"成本预算定价，竞品仅作买家售价参考；同款未确认；有适用的官方费用参考时另列预算售价换算，未做自动跟价或最低价承诺。不是实际净利润。"}
    if fallback_from:
        plan.update(method='COST_BUDGET_REFERENCE_UNAVAILABLE',primary_strategy='FOLLOW_MARKET',fallback_from=fallback_from['id'],
            buyer_margin_floor_verified=False,limitations='跟价参考未取得，按此前授权形成独立成本预算候选；净回款贡献利润率不等于买家售价利润率，利润底线及计算口径待确认，发布前须核对。')
        plan['reference_issues']=copy.deepcopy(fallback_from.get('reference_issues') or [])
        plan['warnings'].extend(r['reason'] for r in plan['reference_issues'])
        plan['warnings'].append(plan['limitations'])
        # Classify local missing costs before any additional market/rate calls.
        eligible=[]
        for sku in skus:
            missing=[]
            from .procurement_v3 import purchase_cost
            try:purchase=purchase_cost(draft,sku)
            except ValueError as exc:purchase=None;missing.append(str(exc))
            for key,raw in [('purchase_cost_cny',purchase),('domestic_shipping_cny',sku.get('domestic_shipping_cny',(draft.get('pricing_plan') or {}).get('domestic_shipping_cny')))]:
                if raw is None:missing.append(key)
            try:package_values(sku,draft)
            except ValueError as exc:missing.append(str(exc))
            if missing:plan['errors'].append(str(sku.get('sku_id'))+'：成本预算缺 '+', '.join(missing))
            else:eligible.append(sku)
        if plan['errors'] and not eligible:
            save(root/'data/v3/pricing/plans'/(plan['id']+'.json'),plan);return plan
    query=str(value.get("query") or "").strip()
    if not query:raise ValueError("请填写目标站点的商品搜索词，不能凭空推断同款")
    comparison={'items':[],'price_bands':[],'state':'REFERENCE_UNAVAILABLE'} if fallback_from else prices.compare(root,dict(store_id=store_id,site_id=site,query=query))
    plan["comparison"]=comparison
    plan["market_reference"]=market_reference(draft,comparison)
    quote=prices.rate_quote({"currency":"USD"})
    plan["exchange_rate"]=quote
    if not quote.get("cny_per_currency"):plan["errors"].append("本次未取得参考汇率")
    if not comparison.get("price_bands"):
        if value.get('allow_cost_only') is True:
            plan['warnings'].append('本次未取得市场在售价格；仅按完整成本与目标利润生成预算建议，发布前须核对。')
            if not fallback_from:plan['method']='COST_TARGET_MARKET_UNAVAILABLE'
        else:plan['errors'].append('本次未取得市场在售价格，保留原价')
    if (draft.get("pricing_plan") or {}).get("mode")!="net_proceeds":plan["errors"].append("此入口仅支持Remote净回款草稿")
    if not plan["errors"] or fallback_from and eligible:
        for sku in eligible if fallback_from else skus:
            sid=str(sku.get("sku_id") or "")
            try:
                sites=[s for s in sku.get("site_pricing",[]) if s.get("site_id")==site]
                if len(sites)!=1 or sites[0].get("logistic_type")!="remote" or sites[0].get("price") is not None:raise ValueError("SKU没有唯一Remote净回款站点配置")
                from .procurement_v3 import purchase_cost
                row=solve(purchase_cost(draft,sku),sku.get('domestic_shipping_cny',(draft.get("pricing_plan") or {}).get("domestic_shipping_cny")),
                  forwarder(sku,draft,p),p.get("packaging_material_cny"),quote["cny_per_currency"],p)
                row.update(sku_id=sid,variant=sku.get("variant"),previous_net_proceeds_usd=sites[0].get("net_proceeds"))
                row["market_assessment"]=market_assessment(root,draft,sku,row,plan["market_reference"],site,store_id)
                plan["rows"].append(row)
            except ValueError as exc:plan["errors"].append(sid+"："+str(exc))
    plan['margin_policy']=margin_policy(p,'NET_PROCEEDS')
    plan['locked_sku_ids']=[str(s['sku_id']) for s in skus if price_locked(draft,s,site)]
    if plan['rows'] and plan['margin_policy']['status']!='CONFIRMED':plan['state']='MARGIN_POLICY_PENDING'
    elif plan["rows"] and not plan["errors"]:plan["state"]="READY_ESTIMATED"
    save(root/"data/v3/pricing/plans"/(plan["id"]+".json"),plan)
    return plan
def apply(root,plan_id,confirmed):
    if confirmed is not True:raise ValueError("需明确确认写入本机预算净回款")
    if not re.fullmatch(r"[a-f0-9]{32}",str(plan_id)):raise ValueError("定价方案编号无效")
    root=Path(root);pp=root/"data/v3/pricing/plans"/(plan_id+".json")
    with LOCK:
        plan=json.loads(pp.read_text(encoding="utf-8"))
        if plan["state"]=="APPLIED_LOCAL":return plan
        basis='BUYER_PRICE' if plan.get('method')=='FOLLOW_REMOTE_MARKET_PRICE' else 'NET_PROCEEDS'
        check=margin_policy(policy(root),basis)
        if check['status']!='CONFIRMED':raise ValueError(('净回款预算尚不能核对已确认的买家售价利润底线' if check['reason']=='BASIS_NOT_COMPARABLE' else '利润底线及计算口径待用户确认')+'；仅保存候选，不写入站点价格')
        if plan["state"]!="READY_ESTIMATED" or not plan.get("rows") or plan.get("errors"):raise ValueError("方案不满足写入条件")
        if not 0<=time.time()-plan["created_epoch"]<=900:raise ValueError("方案已过期，请重新查询定价")
        current_policy=hashlib.sha256(json.dumps(policy(root),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if plan["policy_hash"]!=current_policy:raise ValueError("预算策略已变化，请重新定价")
        prices.operations.store(root,plan["store_id"])
        path=draft_path(root,plan["name"])
        if hashfile(path)!=plan["draft_hash"]:raise ValueError("商品已被修改，请重新定价")
        draft=json.loads(path.read_text(encoding="utf-8"));ev=draft.setdefault("evidence",{})
        if any(price_locked(draft,s,plan['site_id']) for s in ev['sku_details'] if any(str(s['sku_id'])==str(r['sku_id']) for r in plan['rows'])):raise ValueError('SKU价格已人工锁定；保留原价及预算候选')
        selected=str(ev.get("current_pricing_sku_id") or ev["sku_details"][0]["sku_id"])
        for row in plan["rows"]:
            net=prices.number(row["net_proceeds_usd"],"净回款")
            ratio=row.get('sales_margin_pct') if plan.get('method')=='FOLLOW_REMOTE_MARKET_PRICE' else row.get('margin_pct')
            if plan.get('method')=='FOLLOW_REMOTE_MARKET_PRICE' and row.get('floor_passed') is not True:raise ValueError('跟价未达到已确认底线，不能用显示四舍五入值通过')
            if not net or prices.number(ratio,"利润率") is None or prices.number(ratio,"利润率")<prices.number(plan["policy"]["minimum_margin_pct"],"最低利润率"):raise ValueError("净回款或利润下限检查失败")
            sku=next(s for s in ev["sku_details"] if str(s["sku_id"])==row["sku_id"])
            for site_row in sku["site_pricing"]:
                if site_row["site_id"]==plan["site_id"]:
                    site_row["net_proceeds"]=float(net);site_row["price"]=None
            # Global net is a fallback only; do not alter prices configured for other sites.
            if row["sku_id"]==selected:
                draft.setdefault("pricing_plan",{})["target_net_proceeds_usd"]=float(net)
                for site_row in draft["payload"].get("sites_to_sell",[]):
                    if site_row.get("site_id")==plan["site_id"]:site_row.update(net_proceeds=float(net),price=None)
            if plan["site_id"]=="MLM":sku["target_net_proceeds_usd"]=float(net)
            dims,weight=package_values(sku,draft)
            sku.setdefault("autopricing_by_site",{})[plan["site_id"]]={"plan_id":plan_id,"budget":row,"status":"BUDGET_ESTIMATE",
                "input_package":{"length_cm":float(dims[0]),"width_cm":float(dims[1]),"height_cm":float(dims[2]),"weight_g":float(weight)}}
        ev["autopricing"]={"plan_id":plan_id,"store_id":plan["store_id"],"site_id":plan["site_id"],"status":"BUDGET_ESTIMATE","updated_at":prices.stamp()}
        selected_budget=next((r for r in plan["rows"] if r["sku_id"]==selected),None)
        if selected_budget:
            rate=Decimal(plan["exchange_rate"]["cny_per_currency"]);net=Decimal(selected_budget["net_proceeds_usd"])
            selected_sku=next(s for s in ev['sku_details'] if str(s['sku_id'])==selected)
            dims,weight=package_values(selected_sku,draft)
            extras=sum((Decimal(selected_budget[k]) for k in ("purchase_cny","domestic_cny","forwarder_cny","packaging_cny")),Decimal(plan["policy"]["other_cny"]))*pct(plan["policy"],"returns_base_pct")/rate+net*(pct(plan["policy"],"ads_net_pct")+pct(plan["policy"],"tax_reserve_net_pct"))+Decimal(plan["policy"]["other_cny"])/rate
            draft["pricing_plan"].update(exchange_rate_cny_per_usd=float(rate),exchange_rate_source=plan["exchange_rate"]["source"],
              packaging_cost_usd=float(Decimal(selected_budget["packaging_cny"])/rate),other_cost_usd=float(extras),
              purchase_cost_cny=float(Decimal(selected_budget['purchase_cny'])),domestic_shipping_cny=float(Decimal(selected_budget['domestic_cny'])),
              package_weight_g=float(weight),package_length_cm=float(dims[0]),package_width_cm=float(dims[1]),package_height_cm=float(dims[2]),
              forwarder_packing_service_cny=float(Decimal(plan['policy']['packing_service_cny'])),
              forwarder_packaging_material_cny=0,forwarder_value_added_cny=0,forwarder_label_cny=0)
            draft["pricing_plan"]["profit_guidance"]={"status":"BUDGET_ESTIMATE","missing_inputs":[],
              "current_target":{"net_proceeds_usd":float(net),"unit_contribution_usd":selected_budget["unit_contribution_usd"],"margin_pct":selected_budget.get('sales_margin_pct',selected_budget['margin_pct']),"margin_basis":selected_budget.get('margin_basis','NET_PROCEEDS'),"buyer_price_usd":selected_budget.get('buyer_price_usd')},
              "assumptions":selected_budget["assumptions"],"limitations":plan["limitations"]}
        draft.setdefault("pricing_plan",{}).update(status="BUDGET_ESTIMATE",note="已按自主定价预算更新本机净回款；广告、税费现金预留、退货损耗为估计，竞品仅参考，不是平台最终售价或实得净利润。")
        backup=root/"data/v3/pricing/backups"/(plan_id+"-"+path.name)
        backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(path.read_bytes())
        plan.update(state="APPLIED_LOCAL",applied_at=prices.stamp(),backup=str(backup.relative_to(root)))
        save(path,draft);plan["applied_hash"]=hashfile(path);save(pp,plan)
    return plan

def undo(root,plan_id,confirmed):
    if confirmed is not True or not re.fullmatch(r"[a-f0-9]{32}",str(plan_id)):raise ValueError("需确认有效的定价撤销")
    root=Path(root);pp=root/"data/v3/pricing/plans"/(plan_id+".json")
    with LOCK:
        plan=json.loads(pp.read_text(encoding="utf-8"))
        if plan["state"]!="APPLIED_LOCAL":raise ValueError("只有已写入的方案可撤销")
        path=draft_path(root,plan["name"])
        if hashfile(path)!=plan["applied_hash"]:raise ValueError("商品随后已修改，不能覆盖新内容")
        backup=root/"data/v3/pricing/backups"/(plan_id+"-"+path.name)
        if hashfile(backup)!=plan["draft_hash"]:raise ValueError("备份校验失败")
        save(path,json.loads(backup.read_text(encoding="utf-8")))
        plan.update(state="UNDONE",undone_at=prices.stamp());save(pp,plan)
    return plan
