"""Explicit-input contribution calculator and store-bound read-only competitor prices."""
from __future__ import annotations
import json, re, statistics, time, uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from urllib.parse import urlparse
from . import store_operations_v3 as operations
from .mercadolibre_client import MercadoLibreAPIError
from .exchange_rates import fetch_exchange_rate, ExchangeRateError

VERSION = "2026-10-11-autoprice-1"
COSTS = {"purchase":"采购成本","domestic":"国内运费","packaging":"包装材料","forwarder":"货代操作费","ads":"广告分摊","tax":"卖家另付税费","returns":"退货损耗预留","other":"其他变动成本"}
CURRENCIES = {"USD","MXN","CNY","BRL","CLP","COP","ARS"}
SITES = {"MLM","MLB","MLC","MCO","MLA"}

def stamp(): return datetime.now(timezone.utc).isoformat()
def number(value, label):
    if value is None or value == "": return None
    if isinstance(value, bool): raise ValueError(label+"不能是布尔值")
    try: n=Decimal(str(value))
    except (InvalidOperation, ValueError): raise ValueError(label+"需为有效数字")
    if not n.is_finite() or n<0 or n>Decimal("1000000000") or (n and n<Decimal("0.000000001")): raise ValueError(label+"需为0到10亿的有限数字")
    return n
def money(n):
    if n is None: return None
    with localcontext() as ctx:
        ctx.prec=60
        return str(n.quantize(Decimal(".01"),rounding=ROUND_HALF_UP))
def currency(value):
    c=str(value or "").upper()
    if c not in CURRENCIES: raise ValueError("请选择支持的币种")
    return c

def calculate(value):
    if not isinstance(value,dict): raise ValueError("计算参数需为对象")
    mode=value.get("mode","net_proceeds")
    if mode not in {"net_proceeds","sale_price"}: raise ValueError("计算口径无效")
    cur=currency(value.get("currency","USD"))
    income=number(value.get("income"),"收入")
    rate=Decimal(1) if cur=="CNY" else number(value.get("cny_per_currency"),"汇率")
    if rate==0: raise ValueError("汇率必须大于0")
    raw=value.get("costs_cny") or {}
    if not isinstance(raw,dict) or set(raw)-set(COSTS): raise ValueError("成本字段无效")
    costs={k:number(raw.get(k),label) for k,label in COSTS.items()}
    missing=[label for k,label in COSTS.items() if costs[k] is None]
    if income is None: missing.insert(0,"收入")
    if rate is None: missing.append("汇率：1 "+cur+" 等于多少 CNY")
    platform_fee=number(value.get("platform_fee"),"平台佣金")
    platform_shipping=number(value.get("platform_shipping"),"平台物流费")
    if mode=="net_proceeds":
        if platform_fee or platform_shipping: raise ValueError("净回款模式不能再次扣除已含的平台佣金、平台物流费")
        platform_fee=platform_shipping=Decimal(0)
    else:
        if platform_fee is None: missing.append("平台佣金")
        if platform_shipping is None: missing.append("平台物流费")
    selling=number(value.get("selling_price"),"买家售价") if mode=="net_proceeds" else income
    if mode=="net_proceeds" and selling is not None and income is not None and selling<income:
        raise ValueError("同币种买家售价不能小于净回款")
    total_cny=sum((n for n in costs.values() if n is not None),Decimal(0))
    result={"ok":True,"status":"MISSING_INPUTS" if missing else "SCENARIO_ESTIMATE","currency":cur,
      "mode":mode,"income":money(income),"missing_inputs":missing,"costs_cny":{k:money(v) for k,v in costs.items()},
      "known_cost_cny":money(total_cny),"income_margin_label":"净回款贡献利润率" if mode=="net_proceeds" else "销售收入贡献利润率",
      "profit":None,"income_margin_pct":None,"sales_margin_pct":None,"cost_return_pct":None,
      "formula":"贡献利润=输入收入-卖家变动成本；利润率=贡献利润÷对应收入×100%",
      "limitations":"按输入情景估算，未核验实际结算；不含固定开支分摊，不是最终净利润。未知成本留空；不适用需明确填0。净回款已包含的平台扣费不得重复填写。",
      "source":{"calculated_at":stamp(),"type":"USER_INPUT_SCENARIO","fx_note":str(value.get("fx_note") or "用户输入，未核验")[:180]}}
    if missing: return result
    cost=total_cny/rate+platform_fee+platform_shipping
    profit=income-cost
    result.update(variable_cost=money(cost),profit=money(profit),
      income_margin_pct=money(profit/income*100) if income else None,
      sales_margin_pct=money(profit/selling*100) if selling else None,
      cost_return_pct=money(profit/cost*100) if cost else None,
      break_even_income=money(cost),platform_fee=money(platform_fee),platform_shipping=money(platform_shipping))
    return result

def rate_quote(value):
    cur=currency(value.get("currency","USD"))
    try:
        quote=fetch_exchange_rate(cur,"CNY"); rate=number(quote.get("rate"),"参考汇率")
        if not rate: raise ValueError("未取得有效参考汇率")
        return {"ok":True,"currency":cur,"cny_per_currency":str(rate),"source":quote,"note":"参考汇率，不是银行实际结汇汇率；可手动替换。"}
    except (ExchangeRateError,ValueError) as exc: return {"ok":True,"status":"UNAVAILABLE","error":"本次未取得参考汇率："+str(exc)}

def inputs(root):
    products=[]
    directory=Path(root)/"drafts"
    for path in sorted(directory.glob("*-draft.json")):
        if not path.resolve().is_relative_to(directory.resolve()): continue
        try:
            draft=json.loads(path.read_text(encoding="utf-8"));plan=draft.get("pricing_plan") or {}
            products.append({"name":path.name,"title":(draft.get("payload") or {}).get("title") or path.stem,
              "domestic_cny":plan.get("domestic_shipping_cny"),
              "skus":[{"id":str(s.get("sku_id") or ""),"variant":s.get("variant"),
                "purchase_cny":s.get("purchase_cost_cny"),"net_proceeds_usd":s.get("target_net_proceeds_usd"),
                "sites":s.get("site_pricing") or []} for s in (draft.get("evidence") or {}).get("sku_details",[]) if isinstance(s,dict)]})
        except (OSError,ValueError,TypeError): continue
    return {"ok":True,"products":products,"stores":[{k:s.get(k) for k in ("id","alias","merchant_id")} for s in operations.registry(root)],
      "cost_labels":COSTS,"version":VERSION,"note":"商品内的历史成本与暂定净回款仅供带入；计算前核对，不覆盖商品。"}

def item_ids(raw,site):
    values=raw if isinstance(raw,list) else re.split(r"[\s,，;；]+",str(raw or "").strip())
    found=[]
    for value in values:
        if not value: continue
        value=str(value).strip()
        if value.startswith(("http://","https://")):
            u=urlparse(value)
            domains=("mercadolibre.com.mx","mercadolivre.com.br","mercadolibre.cl","mercadolibre.com.co","mercadolibre.com.ar")
            if u.scheme!="https" or not any(u.hostname==d or (u.hostname or "").endswith("."+d) for d in domains): raise ValueError("仅接受美客多官方商品链接或刊登编号")
            match=re.search(r"(MLM|MLB|MLC|MCO|MLA)-(\d{6,15})(?:-|/|$)",u.path.upper())
            if not match: raise ValueError("链接没有可识别的刊登编号；目录 /p/ 链接请换为具体商品刊登编号")
            value="".join(match.groups())
        value=value.upper().replace("-","")
        if not re.fullmatch(r"(MLM|MLB|MLC|MCO|MLA)\d{6,15}",value) or not value.startswith(site): raise ValueError("刊登编号需与所选站点一致")
        if value not in found: found.append(value)
    if len(found)>10: raise ValueError("单次最多10个刊登编号")
    return found

def item_row(item,site,endpoint):
    if not isinstance(item,dict): raise ValueError("商品响应格式变化")
    item_id=str(item.get("id") or "")
    if not re.fullmatch(site+r"\d{6,15}",item_id) or item.get("site_id",site)!=site: raise ValueError("接口返回商品与所选站点不一致")
    cur=str(item.get("currency_id") or "").upper()
    price=number(item.get("price"),"刊登价格")
    link=str(item.get("permalink") or "")
    u=urlparse(link)
    safe_link=u.scheme=="https" and any(u.hostname==d or (u.hostname or "").endswith("."+d) for d in ("mercadolibre.com.mx","mercadolivre.com.br","mercadolibre.cl","mercadolibre.com.co","mercadolibre.com.ar"))
    return {"id":item_id,"title":str(item.get("title") or "")[:250],"price":money(price),"currency":cur,
      "condition":str(item.get("condition") or "unknown"),"status":str(item.get("status") or "unknown"),
      "category_id":item.get("category_id"),"free_shipping":(item.get("shipping") if isinstance(item.get("shipping"),dict) else {}).get("free_shipping"),
      "permalink":link if safe_link else None,"endpoint":endpoint,"spec_match":"NOT_VERIFIED"}

def compare(root,value):
    site=str(value.get("site_id") or "MLM").upper()
    if site not in SITES: raise ValueError("站点无效")
    store_id=str(value.get("store_id") or "")
    store=operations.store(root,store_id)
    ids=item_ids(value.get("item_ids"),site)
    query=str(value.get("query") or "").strip()
    if not ids and not 1<=len(query)<=160: raise ValueError("请输入1–160字关键词，或具体刊登编号")
    baseline=number(value.get("reference_price"),"参考买家售价")
    baseline_cur=currency(value.get("reference_currency","MXN"))
    result={"ok":True,"state":"UNAVAILABLE","store_id":store_id,"site_id":site,"query":query,"items":[],
      "errors":[],"price_bands":[],"platform_queried_at":stamp(),"source":"Mercado Libre official GET API",
      "limitations":"关键词返回的是候选商品，未核对同款、规格和套装数量。刊登价不是成交价；包邮不等于含税到手价。不同币种和成色分组，不直接混比。参考价必须是买家售价，不能填净回款。"}
    rows=result["items"]; started=time.monotonic()
    try:
        client,_=operations.client_for(root,store)
        if ids:
            for item_id in ids:
                if time.monotonic()-started>45:
                    result["errors"].append({"error":"查询达到时间上限，余下编号本次未取得"});break
                errors=[]; item=None
                for prefix in ("/items/","/marketplace/items/"):
                    endpoint=prefix+item_id
                    try:
                        response=client.get(endpoint)
                        if not isinstance(response,dict) or str(response.get("id"))!=item_id: raise ValueError("接口返回编号不一致")
                        item=item_row(response,site,endpoint);break
                    except (MercadoLibreAPIError,ValueError,TimeoutError,OSError) as exc: errors.append(str(exc)[:200])
                if item is not None: rows.append(item)
                else: result["errors"].append({"id":item_id,"error":"本次未取得；"+"；".join(errors)})
        else:
            try:
                response=client.search_competitors(site,query,10)
                if not isinstance(response,dict) or not isinstance(response.get("results"),list): raise ValueError("搜索响应格式变化，本次未取得列表")
                for item in response["results"][:10]:
                    try: rows.append(item_row(item,site,"/sites/"+site+"/search"))
                    except ValueError as exc: result["errors"].append({"error":str(exc)})
            except (MercadoLibreAPIError,OSError,TimeoutError) as exc:
                if value.get("allow_public_fallback") is False: raise
                result["attempts"]=[{"source":"official_listing_search","http_status":getattr(exc,"http_status",None),"error":str(exc)[:150]}]
                from .market_prices_v3 import public_search
                rows.extend(public_search(site,query))
                result["source"]="Mercado Libre public search HTML (official API unavailable)"
                result["limitations"]+=" 备用来源为无需登录的公开搜索页；无邮编，包邮提示和价格可能因位置、促销及登录状态变化，未取得结账含税到手价。"
        groups={}
        for row in rows:
            row["reference_gap_pct"]=None
            if row["price"] is not None and baseline and row["currency"]==baseline_cur:
                row["reference_gap_pct"]=money((baseline-Decimal(row["price"]))/Decimal(row["price"])*100) if Decimal(row["price"])>0 else None
            if row["price"] is not None and Decimal(row["price"])>0 and row["currency"] in CURRENCIES and row["status"] not in {"closed","paused","inactive"}:
                groups.setdefault((row["currency"],row["condition"]),[]).append(Decimal(row["price"]))
        for (cur,condition),prices in groups.items():
            result["price_bands"].append({"currency":cur,"condition":condition,"count":len(prices),
              "min":money(min(prices)),"median":money(statistics.median(prices)),"max":money(max(prices))})
        result["state"]=("PARTIAL" if result["errors"] else "READY") if rows else ("UNAVAILABLE" if result["errors"] else "NO_RESULTS")
        if rows and not result["price_bands"]:
            result["state"]="PARTIAL"
            result["errors"].append({"error":"已取得商品，但未取得可用的在售价格，不能计算价格区间"})
    except (MercadoLibreAPIError,ValueError,TimeoutError,OSError) as exc:
        result["errors"].append({"error":"本次未取得："+str(exc)[:250],"http_status":getattr(exc,"http_status",None)})
    result["completed_at"]=stamp()
    result["next_step"]="关键词接口受限时自动读取公开搜索页；若出现验证或价格缺失则停止。具体编号查询仍受平台权限限制。"
    directory=Path(root)/"data/v3/pricing"/store_id
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(site+"-latest.json");tmp=path.with_name(uuid.uuid4().hex+".tmp")
    tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8");tmp.replace(path)
    return result
