"""Explicit-store publishing. Preview is local/read-only; commit consumes a frozen receipt."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from . import publish_service as service
from . import store_operations_v3 as operations

LOCK = threading.RLock()
VERSION = "2026-10-11-target-publish-1"
AUTO_REFRESH_DELAYS = (20, 60, 180)
AUTO_REFRESH_MAX_AGE = 24 * 3600
Error = service.PublishError

def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""

def name_ok(name):
    if not isinstance(name, str) or not re.fullmatch(r"[^/\\:]+-draft\.json", name):
        raise Error("草稿文件名无效")
    return name

def store_row(root, store_id):
    if not isinstance(store_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", store_id):
        raise Error("请明确选择目标店铺")
    row = operations.store(root, store_id)
    if row.get("auth_type") != "CBT":
        raise Error("此入口用于 CBT 店铺，请选择全球销售店铺")
    return row

def public_store(row):
    return {key: row.get(key) for key in ("id", "alias", "merchant_id", "auth_type")}

def record_path(root, name, store_id):
    name_ok(name)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", str(store_id)):
        raise Error("店铺编号无效")
    return root/"data/v3/publishing/records"/store_id/(Path(name).stem+".json")

def record(root, name, store_id):
    return read(record_path(root, name, store_id), {})

def decorate(root, row, store_id=""):
    result = dict(row)
    paths = [record_path(root, row["name"], store_id)] if store_id else sorted(
        (root/"data/v3/publishing/records").glob("*/"+Path(name_ok(row["name"])).stem+".json"))
    records = [read(p, {}) for p in paths if p.is_file()]
    if records:
        current = max(records, key=lambda x: x.get("updated_at", 0))
        result.update(publish_status=current["status"], publish_error=current.get("error"),
                      publish_variants=current.get("variants", []), publish_checked_at=current.get("checked_at"),
                      publish_auto_refresh_attempts=current.get("auto_refresh_attempts", 0),
                      publish_auto_refresh_next_at=current.get("auto_refresh_next_at"),
                      publish_store=current.get("store"), publish_bound=True,
                      publish_records=[{"store":r.get("store"), "status":r.get("status")} for r in records])
    elif result.get("publish_status") != "pending":
        result["publish_legacy_unbound"] = True
    return result

def _local_issues(root, name):
    # Deferred import avoids the workbench's circular module dependency.
    from .workbench_v3 import draft_rows
    row = next((r for r in draft_rows(root) if r["name"] == name), None)
    if row is None:
        raise Error("没有找到商品")
    issues = []
    if not row.get("in_publish_center"):
        issues.append("AI 处理尚未完成，请先完成商品处理")
    for field in ("errors", "publish_issues", "evidence_issues"):
        issues.extend(row.get(field) or [])
    return list(dict.fromkeys(issues))

def _supplier_inventory_issues(draft):
    issues = []
    for sku in service._sku_rows(draft):
        source = str(sku.get("inventory_source") or "").upper()
        if source in {"1688_SKU_AVAILABLE_QUANTITY", "EXISTING_SUPPLIER_CAPTURE", "DEFAULT_1000_WHEN_1688_UNAVAILABLE"}:
            issues.append("可售库存仍取自货源页面或旧默认值；请将该 SKU 改为本店默认 200 件后重新点击发布")
    return issues

def _requests(draft, previous, retry):
    pictures = {str(s.get("sku_id") or f"sku-{i}"): ["UPLOAD_REQUIRED"] * len(service._gallery_for_sku(draft, str(s.get("sku_id") or f"sku-{i}")))
                for i, s in enumerate(service._sku_rows(draft), 1)}
    payload = service.build_user_product_payload(draft, pictures)
    if retry:
        failed = {r.get("seller_sku"): {s["site_id"] for s in r.get("sites", []) if s.get("status") == "failed" and not s.get("item_id")}
                  for r in previous.get("variants", [])}
        payload = [dict(p, sites_to_sell=[s for s in p["sites_to_sell"] if s["site_id"] in failed.get(service._seller_sku(p), set())]) for p in payload]
        payload = [p for p in payload if p["sites_to_sell"]]
    return payload

def _local_site_titles(draft):
    payload = draft.get("payload") or {}
    result = []
    for sku in service._sku_rows(draft):
        source = sku.get("site_pricing") or payload.get("sites_to_sell") or []
        for raw in source:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if title:
                result.append({"seller_sku": str(sku.get("seller_sku") or payload.get("seller_sku") or ""),
                               "site_id": str(raw.get("site_id") or "").strip(), "title": title})
    return result


def _account_checks(root, row, payload):
    api, me = operations.client_for(root, row)
    if str(me.get("id")) != str(row.get("merchant_id")):
        raise Error("授权身份与目标店铺不一致，已停止")
    if "user_product_seller" not in (me.get("tags") or []):
        raise Error("目标店铺未启用 User Products 发布模型")
    if ((me.get("status") or {}).get("sell") or {}).get("allow") is not True:
        raise Error("本次未取得账号允许销售的确认")
    enabled = operations.mappings(api, row)
    limits = api.get("/marketplace/users/cap")
    if not isinstance(limits, list):
        raise Error("本次未取得目标站点刊登额度")
    required = Counter((s["site_id"], s["logistic_type"]) for p in payload for s in p["sites_to_sell"])
    checks = []
    for (site, logistics), quantity in required.items():
        mapping = next((m for m in enabled if m.get("site_id") == site and m.get("logistic_type") == logistics), None)
        cap = next((c for c in limits if c.get("site_id") == site and c.get("logistic_type") == logistics), None)
        if not mapping:
            raise Error(f"目标店铺没有 {site} / {logistics} 发布权限")
        if not cap or str(cap.get("user_id")) != str(mapping.get("user_id")):
            raise Error(f"本次未取得 {site} / {logistics} 的有效刊登额度")
        quota, total = cap.get("quota"), cap.get("total_items")
        if not isinstance(quota, (int, float)) or not isinstance(total, (int, float)) or min(quota, total) < 0:
            raise Error("平台刊登额度格式异常")
        if quota and total + quantity > quota:
            raise Error(f"{site} / {logistics} 刊登额度不足：当前 {total}，上限 {quota}，本次 {quantity}")
        checks.append({"site_id":site,"logistic_type":logistics,"quota":quota,"total_items":total,"requested":quantity})
    return api, checks

def _prior_guard(root, name, previous):
    legacy = read(root/"data/publish_records"/(Path(name).stem+".json"), {})
    if legacy and not previous:
        raise Error("旧发布记录未绑定店铺，需先核对平台记录，不能再次创建")
    if previous.get("status") in {"success", "pending_confirmation", "submitting"}:
        raise Error("已有成功或结果待确认的发布记录，请回查，不能再次创建")
    if previous.get("variants") and not previous.get("creation_rejected"):
        if any(s.get("status") == "pending_confirmation" for r in previous["variants"] for s in r.get("sites", [])):
            raise Error("平台结果尚不明确，请先回查")
        for r in previous["variants"]:
            if any(s.get("status") == "failed" and not s.get("item_id") for s in r.get("sites", [])) and not r.get("siteless_user_product_id"):
                raise Error("失败站点缺少 User Product ID，需平台核对，不能安全重试")

def _freeze(root, plan_id, draft):
    frozen, hashes = copy.deepcopy(draft), {}
    review = frozen.setdefault("evidence", {}).setdefault("image_review", {})
    galleries = []
    for i, sku in enumerate(service._sku_rows(draft), 1):
        sku_id = str(sku.get("sku_id") or f"sku-{i}")
        gallery = []
        for n, image in enumerate(service._gallery_for_sku(draft, sku_id)):
            data, filename, _ = service._image_bytes(image, root)
            if not data or len(data) > 10_000_000:
                raise Error("图片为空或超过 10 MB")
            suffix = Path(filename).suffix.lower()
            if suffix not in {".png",".jpg",".jpeg",".webp"}:
                raise Error("上架图片格式需为 PNG、JPG 或 WebP")
            relative = f"publish-plans/{plan_id}/{i}-{n}{suffix}"
            p = root/"assets"/relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            hashes[relative] = digest(p)
            gallery.append({"local_file":relative})
        galleries.append({"sku_id":sku_id,"gallery":gallery})
    review["sku_listing_galleries"] = galleries
    if len(galleries) == 1:
        review["gallery"] = galleries[0]["gallery"]
    return frozen, hashes

def prepare(root, name, store_id):
    name_ok(name)
    row = store_row(root, store_id)
    source = root/"drafts"/name
    if not source.is_file():
        raise Error("没有找到草稿")
    source_hash = digest(source)
    draft, previous = read(source), record(root, name, store_id)
    issues, request, checks, category = _local_issues(root, name) + _supplier_inventory_issues(draft), [], [], {}
    from .integrated_pricing_v3 import summary as pricing_summary
    budget_summary=pricing_summary(draft,root)
    if budget_summary['state'] in ('STALE','BLOCKED','PARTIAL_BUDGET_ESTIMATE','MARGIN_POLICY_PENDING'):issues.extend(budget_summary.get('errors') or ['利润门槛及计算口径尚未确认，请先核对预算候选'])
    from .autopricing_v3 import policy as pricing_policy
    active_policy=pricing_policy(root)
    if (root/'config/autopricing_policy.json').is_file() and active_policy.get('margin_policy_status')!='CONFIRMED':
        issues.append('利润底线及计算口径待确认；预算候选不能自动放行发布')
    if active_policy.get('margin_policy_status')=='CONFIRMED' and active_policy.get('confirmed_margin_basis')=='BUYER_PRICE' and budget_summary['state'] in ('NOT_RUN','MANUAL_PRICE_LOCKED'):
        issues.append('买家售价贡献利润率尚未核对；人工价格保持不变，需完整成本及售价依据确认已达到所设底线')
    if budget_summary['state']=='BUDGET_ESTIMATE' and budget_summary.get('store_id')!=store_id:issues.append('当前利润定价绑定其他店铺，请重新核价')
    retry = bool(previous.get("variants") and not previous.get("creation_rejected"))
    try:
        _prior_guard(root, name, previous)
        request = _requests(draft, previous, retry)
        if not request:
            raise Error("没有可提交的 SKU 或失败站点")
        api, checks = _account_checks(root, row, request)
        category = service.validate_category_sites(draft, api)
        issues.extend(category.get("issues") or [])
    except Exception as exc:
        issues.append(str(exc)[:600])
    plan_id = uuid.uuid4().hex
    frozen, image_hashes = draft, {}
    if not issues:
        try:
            frozen, image_hashes = _freeze(root, plan_id, draft)
            if digest(source) != source_hash:
                raise Error("核对期间商品被修改，请重新点击发布")
        except Exception as exc:
            issues.append(str(exc)[:600])
    from .integrated_pricing_v3 import summary as pricing_summary
    image_sets = []
    if image_hashes:
        for index, sku in enumerate(service._sku_rows(frozen), 1):
            sku_id = str(sku.get("sku_id") or f"sku-{index}")
            files = [image["local_file"] for image in service._gallery_for_sku(frozen, sku_id)]
            image_sets.append({"sku_id":sku_id, "seller_sku":sku.get("seller_sku") or sku_id,
                               "count":len(files), "files":files})
    from .autopricing_v3 import policy_fingerprint
    plan = {"pricing_policy_hash":policy_fingerprint(root), "ai_processing_pricing":pricing_summary(draft), "id":plan_id, "name":name, "store":public_store(row), "source_hash":source_hash,
            "previous_hash":digest(record_path(root,name,store_id)), "created_at":time.time(),
            "expires_at":time.time()+900, "state":"blocked" if issues else "ready",
            "valid":not issues, "issues":list(dict.fromkeys(issues)), "request":request,
            "category_validation":category, "capacity":checks, "retry_failed_only":retry,
            "frozen_draft":frozen, "image_hashes":image_hashes, "image_sets":image_sets,
            "title":(draft.get("payload") or {}).get("title"),
            "site_title_suggestions":_local_site_titles(draft),
            "note":"打开此确认页不会上传图片或提交刊登；当前由平台根据商品信息生成展示标题，本地站点建议标题不随请求提交，发布后须回查。平台最终校验以真实提交结果为准。"}
    save(root/"data/v3/publishing/plans"/(plan_id+".json"), plan)
    return {"ok":True, "preview":{k:v for k,v in plan.items() if k not in {"frozen_draft","source_hash","previous_hash"}},
            "publishing_performed":False}

def reconcile_recorded_rejection(root, current):
    """Use exact frozen request + last explicit rejection; never infer from age."""
    if current.get('request_in_flight') or current.get('outcome_unknown'):
        return False
    variants = current.get('variants') or []
    if not variants or any(v.get('siteless_user_product_id') or v.get('siteless_family_id')
            or any(s.get('item_id') for s in v.get('sites') or []) for v in variants):
        return False
    attempts = current.get('attempts') or []
    if not attempts:return False
    attempt = attempts[-1]
    plan_id = attempt.get('plan_id')
    if (not isinstance(plan_id,str) or not re.fullmatch('[a-f0-9]{32}',plan_id)
            or current.get('plan_id') != plan_id
            or attempt.get('endpoint') != '/global/user-products/families'):
        return False
    plan = read(Path(root)/'data/v3/publishing/plans'/(plan_id+'.json'),{})
    request, response = plan.get('request') or [], attempt.get('response')
    if (plan.get('name') != current.get('draft') or plan.get('store') != current.get('store')
            or not isinstance(response,list) or not request or len(response)!=len(request)
            or not all(service._definite_rejection(r) for r in response)):
        return False
    rebuilt = service._attempt_variants(request,response)
    for rebuilt_variant, requested in zip(rebuilt,request):
        for site in rebuilt_variant['sites']:
            site['logistic_type'] = next(s.get('logistic_type','remote') for s in requested['sites_to_sell'] if s['site_id']==site['site_id'])
    scope = lambda rows: sorted((v.get('seller_sku'),tuple(sorted((s.get('site_id'),s.get('logistic_type','remote')) for s in v.get('sites') or []))) for v in rows)
    if scope(rebuilt)!=scope(variants):return False
    for old,new in zip(variants,rebuilt):
        if old.get('seller_sku')!=new.get('seller_sku'):return False
    current.update(variants=rebuilt,creation_rejected=True,status='failed',
                   error=service._record_status(rebuilt)[1],rejection_reconciled=True)
    return True


def _refresh(root, name, row, api):
    path = record_path(root, name, row["id"])
    current = read(path, {})
    if not current or current.get("store", {}).get("merchant_id") != row.get("merchant_id"):
        raise Error("未取得与目标店铺身份一致的发布记录")
    reconcile_recorded_rejection(root,current)
    if current.get("request_in_flight"):
        # A crash or timeout cannot establish that the platform rejected the write.
        current["outcome_unknown"] = True
    for variant in current.get("variants", []):
        up_id = str(variant.get("siteless_user_product_id") or "")
        if up_id and any(not s.get("item_id") for s in variant.get("sites", [])):
            try:
                if not re.fullmatch(r"(?:CBT)?U[0-9]+", up_id):
                    raise Error("平台 User Product ID 格式异常")
                mapping = api.get(f"/marketplace/user-products/{up_id}/mapping")
                if not isinstance(mapping, list):
                    raise Error("本次未取得有效 User Product 映射")
                for product in mapping:
                    if product.get("siteless_user_product_id") != up_id:
                        continue
                    for actual in product.get("site_items") or []:
                        for site in variant.get("sites", []):
                            if (site["site_id"] == actual.get("site_id") and
                                site.get("logistic_type", "remote") == actual.get("logistic_type", "remote") and actual.get("item_id")):
                                site.update(item_id=actual["item_id"], status="pending_confirmation")
            except Exception as exc:
                variant["check_error"] = str(exc)[:500]
        for site in variant.get("sites", []):
            item_id = str(site.get("item_id") or "")
            if not item_id:
                continue
            try:
                if not re.fullmatch(r"(MLM|MLB|MLC|MCO|MLA)[0-9]+", item_id):
                    raise Error("平台商品 ID 格式异常")
                item = api.get("/marketplace/items/"+item_id)
                if item.get("id") != item_id or item.get("site_id") != site["site_id"]:
                    raise Error("平台回查商品身份不一致")
                state = str(item.get("status") or "")
                site.update(status="success" if state == "active" else "pending_confirmation",
                            marketplace_status=state or "unknown", checked_at=time.time(),
                            marketplace_title=str(item.get("title") or "")[:300],
                            marketplace_currency_id=str(item.get("currency_id") or "")[:10],
                            marketplace_permalink=str(item.get("permalink") or "")[:1000])
                price = item.get("price")
                if isinstance(price, (int, float)) and not isinstance(price, bool) and price >= 0:
                    site["marketplace_price"] = price
                if state != "active":
                    site["check_error"] = "商品已创建，但平台状态为："+(state or "未取得")
                else:
                    site.pop("check_error",None)
                    site.pop("error",None)
                    try:
                        description = api.get("/items/"+item_id+"/description")
                        if not isinstance(description, dict):
                            raise Error("平台描述格式异常")
                        site["marketplace_description"] = str(description.get("plain_text") or description.get("text") or "")[:5000]
                        site["content_checked_at"] = time.time()
                        site.pop("content_check_error", None)
                    except Exception as exc:
                        site["content_check_error"] = str(exc)[:500]
            except Exception as exc:
                site.update(status="pending_confirmation",check_error=str(exc)[:500])
    state, error = service._record_status(current.get("variants", []))
    # No identifiers cannot be reconciled automatically. Never downgrade this to retryable failed.
    if current.get("outcome_unknown"):
        confirmed = bool(current.get("variants")) and all(
            v.get("sites") and all(s.get("status") == "success" and s.get("item_id")
                                   for s in v["sites"])
            for v in current["variants"])
        if confirmed:
            current["outcome_unknown"] = False
            current["request_in_flight"] = False
        else:
            state = "pending_confirmation"
            error = "提交结果未能完整确认；需核对平台记录，禁止重复创建"
    current.update(status=state,error=error,checked_at=time.time(),updated_at=time.time())
    if state != "pending_confirmation":
        current["auto_refresh_next_at"] = None
    save(path,current)
    return current

def refresh(root, name, store_id):
    with LOCK:
        row = store_row(root, store_id)
        api, me = operations.client_for(root,row)
        if str(me.get("id")) != str(row.get("merchant_id")):
            raise Error("店铺身份不一致")
        return {"ok":True,"record":_refresh(root,name,row,api),"publishing_performed":False}

def auto_refresh(root, name, store_id):
    """Retry read-only reconciliation at most three times; never create a listing."""
    with LOCK:
        row = store_row(root, store_id)
        path = record_path(root, name, store_id)
        current = read(path, {})
        if not current or current.get("store", {}).get("merchant_id") != row.get("merchant_id"):
            raise Error("未取得与目标店铺身份一致的发布记录")
        attempts = int(current.get("auto_refresh_attempts") or 0)
        now = time.time()
        started = float(current.get("auto_refresh_started_at") or current.get("updated_at") or now)
        base = float(current.get("checked_at") or current.get("updated_at") or now)
        due = float(current.get("auto_refresh_next_at") or base + AUTO_REFRESH_DELAYS[0])
        identifiers = any(v.get("siteless_user_product_id") or
                          any(s.get("item_id") for s in v.get("sites", []))
                          for v in current.get("variants", []))
        if (current.get("status") != "pending_confirmation" or
            attempts >= len(AUTO_REFRESH_DELAYS) or
            now - started > AUTO_REFRESH_MAX_AGE or not identifiers or now < due):
            return {"ok": True, "record": current, "auto_refresh_performed": False}
        current["auto_refresh_started_at"] = started
        current["auto_refresh_attempts"] = attempts + 1
        current["auto_refresh_next_at"] = (now + AUTO_REFRESH_DELAYS[attempts + 1]
                                            if attempts + 1 < len(AUTO_REFRESH_DELAYS) else None)
        save(path, current)  # Count before the remote read, including a timeout or crash.
        try:
            api, me = operations.client_for(root, row)
            if str(me.get("id")) != str(row.get("merchant_id")):
                raise Error("店铺身份不一致")
            checked = _refresh(root, name, row, api)
            checked.pop("auto_refresh_error", None)
            save(path, checked)
            return {"ok": True, "record": checked, "auto_refresh_performed": True}
        except Exception as exc:
            current = read(path, {})
            current["auto_refresh_error"] = str(exc)[:500]
            save(path, current)
            return {"ok": True, "record": current, "auto_refresh_performed": True}


def sweep_pending(root):
    """Reconcile bound pending receipts even when the publish page is closed."""
    checked = 0
    directory = root / "data/v3/publishing/records"
    for path in directory.glob("*/*.json"):
        try:
            current = read(path, {})
            if not isinstance(current, dict):
                continue
            bound_store = current.get("store")
            if not isinstance(bound_store, dict):
                continue
            name, store_id = current.get("draft"), path.parent.name
            if (current.get("status") != "pending_confirmation" or
                bound_store.get("id") != store_id or
                record_path(root, name, store_id) != path):
                continue
            if auto_refresh(root, name, store_id)["auto_refresh_performed"]:
                checked += 1
        except (Error, OSError, ValueError, TypeError, KeyError):
            # An invalid record must not prevent other stores from reconciling.
            continue
    return checked


def commit(root, plan_id, confirmed, budget_review_confirmed=False):
    if confirmed is not True:
        raise Error("需要勾选确认具体商品、店铺、价格、库存和各 SKU 上架图片")
    if not isinstance(plan_id,str) or not re.fullmatch(r"[a-f0-9]{32}",plan_id):
        raise Error("发布确认已失效，请重新点击发布")
    with LOCK:
        p = root/"data/v3/publishing/plans"/(plan_id+".json")
        plan = read(p,{})
        if plan.get("state") != "ready" or not plan.get("valid") or time.time() >= plan.get("expires_at",0):
            raise Error("核对结果已过期、未通过或已使用，请重新点击发布")
        from .autopricing_v3 import policy_fingerprint
        if plan.get('pricing_policy_hash')!=policy_fingerprint(root):
            raise Error('利润政策已变化或旧计划缺少政策版本，请重新核对后再发布')
        if (plan.get("ai_processing_pricing") or {}).get("requires_budget_review") and budget_review_confirmed is not True:
            raise Error("请明确核对预算利润、成本假设及市场价格缺项后再提交")
        name, store_id = name_ok(plan["name"]), plan["store"]["id"]
        row = store_row(root,store_id)
        if public_store(row) != plan["store"]:
            raise Error("目标店铺登记已变化，请重新点击发布")
        path = record_path(root,name,store_id)
        if digest(root/"drafts"/name) != plan["source_hash"] or digest(path) != plan["previous_hash"]:
            raise Error("商品或发布记录已变化，请重新点击发布")
        for relative, expected in plan["image_hashes"].items():
            image = (root/"assets"/relative).resolve()
            if not image.is_relative_to((root/"assets/publish-plans"/plan_id).resolve()) or digest(image) != expected:
                raise Error("待发布图片已变化，请重新点击发布")
        issues = _local_issues(root,name)
        if issues:
            raise Error("商品状态已变化："+issues[0])
        previous = read(path,{})
        _prior_guard(root,name,previous)
        draft, retry = plan["frozen_draft"], plan["retry_failed_only"]
        api, _ = _account_checks(root,row,plan["request"])
        category = service.validate_category_sites(draft,api)
        if not category["valid"]:
            raise Error("平台类目状态已变化，请重新点击发布")
        if digest(root/"drafts"/name) != plan["source_hash"] or digest(path) != plan["previous_hash"]:
            raise Error("核对期间商品或发布记录变化，请重新点击发布")
        # Persist the consumed receipt and journal BEFORE any remote write.
        plan["state"] = "consumed"; save(p,plan)
        current = dict(previous)
        current.update(store=plan["store"],draft=name,plan_id=plan_id,status="submitting",
                       updated_at=time.time(),request_in_flight=False,outcome_unknown=False,
                       creation_rejected=False)
        save(path,current)
        cache = dict(previous.get("upload_cache") or {})
        posting = False
        try:
            payload = service.build_user_product_payload(draft,service._upload_pictures(draft,root,api,cache))
            if retry:
                failed = {service._seller_sku(x):{s["site_id"] for s in x["sites_to_sell"]} for x in plan["request"]}
                payload = [dict(x,sites_to_sell=[s for s in x["sites_to_sell"] if s["site_id"] in failed.get(service._seller_sku(x),set())]) for x in payload]
                payload = [x for x in payload if x["sites_to_sell"]]
            groups = [[x] for x in payload] if retry else [payload]
            for group in groups:
                prior = next((r for r in current.get("variants",[]) if r.get("seller_sku") == service._seller_sku(group[0])),{})
                endpoint = "/global/user-products/"+str(prior.get("siteless_user_product_id") or "") if retry else "/global/user-products/families"
                if retry and not prior.get("siteless_user_product_id"):
                    raise Error("缺少已创建 User Product ID，不能重试")
                before_variants = copy.deepcopy(current.get("variants",[]))
                pending = service._attempt_variants(group,[])
                # Replace only the attempted sites with pending BEFORE issuing the request.
                current["variants"] = service._merge_variants(before_variants,pending)
                current.update(upload_cache=cache,request_in_flight=True,updated_at=time.time())
                save(path,current)
                posting = True
                response = api.post_json(endpoint,{"sites_to_sell":group[0]["sites_to_sell"]} if retry else group)
                latest = service._attempt_variants(group,response)
                for variant, request in zip(latest,group):
                    if retry and not variant.get("siteless_user_product_id"):
                        variant["siteless_user_product_id"] = prior["siteless_user_product_id"]
                    for site in variant["sites"]:
                        site["logistic_type"] = next(s["logistic_type"] for s in request["sites_to_sell"] if s["site_id"] == site["site_id"])
                        if site["status"] == "success":
                            site["status"] = "pending_confirmation"
                current["variants"] = service._merge_variants(before_variants,latest)
                current.setdefault("attempts",[]).append({"plan_id":plan_id,"endpoint":endpoint,"response":response,"time":time.time()})
                current.update(request_in_flight=False,updated_at=time.time())
                save(path,current)
                posting = False
            return {"ok":True,"record":_refresh(root,name,row,api),"publishing_performed":True}
        except Exception as exc:
            http_status = getattr(exc,"http_status",None)
            rejected = posting and http_status in {400,401,403,404,422,429}
            if rejected:
                current["variants"] = before_variants
                current["creation_rejected"] = not bool(before_variants)
            unknown = posting and not rejected
            current.update(status="pending_confirmation" if unknown else ("partial" if any(s.get("status")=="success" for r in current.get("variants",[]) for s in r.get("sites",[])) else "failed"),
                           outcome_unknown=unknown,request_in_flight=unknown,upload_cache=cache,
                           updated_at=time.time(),error=str(exc)[:1000])
            save(path,current)
            return {"ok":True,"record":current,"publishing_performed":posting,
                    "warning":"平台结果不明，禁止重复创建，请先回查" if unknown else "提交未完成，请查看具体错误"}
