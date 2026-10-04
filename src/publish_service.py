"""Human-confirmed Mercado Libre User Products publishing."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .mercadolibre_client import MercadoLibreAPIError, MercadoLibreCategoryClient, MercadoLibreClient, write_json
except ImportError:
    from mercadolibre_client import MercadoLibreAPIError, MercadoLibreCategoryClient, MercadoLibreClient, write_json


class PublishError(ValueError):
    pass


def _domain_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split("-", 1)[1] if "-" in text else text


def _semantic_category_issue(title: str, domain_key: str) -> str | None:
    """Catch high-confidence product/category contradictions."""
    normalized = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    manual_recoil = bool(re.search(r"\b(recoil starter|pull starter|pull start)\b", normalized))
    electric_starter = bool(re.search(r"\b(starter motor|electric starter|starter solenoid)\b", normalized))
    if manual_recoil and domain_key != "GARDEN_MACHINE_RECOIL_STARTERS":
        return "标题明确是手拉/回弹启动器，不能使用电启动马达类目；应选择 Recoil Starters 类目"
    if electric_starter and domain_key == "GARDEN_MACHINE_RECOIL_STARTERS":
        return "标题明确是电启动马达，不能使用手拉/回弹启动器类目"
    return None


def select_category_candidate(title: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer an explicit product-type match over the predictor's generic first row."""
    if not candidates:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    expected = None
    if re.search(r"\b(recoil starter|pull starter|pull start)\b", normalized):
        expected = "GARDEN_MACHINE_RECOIL_STARTERS"
    elif re.search(r"\b(starter motor|electric starter|starter solenoid)\b", normalized):
        expected = "VEHICLE_STARTERS"
    if expected:
        matched = next((row for row in candidates if _domain_key(row.get("domain_id")) == expected), None)
        if matched:
            return matched
    return candidates[0]


def validate_category_sites(draft: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
    """Validate the CBT category and its live destination-site mappings."""
    payload = draft.get("payload") or {}
    category_id = str(payload.get("category_id") or "").strip().upper()
    sites = sorted({str((row if isinstance(row, dict) else {"site_id": row}).get("site_id") or "").upper()
                    for row in payload.get("sites_to_sell") or []} - {""})
    result: dict[str, Any] = {"category_id": category_id, "valid": False, "issues": [], "warnings": [], "sites": []}
    if not re.fullmatch(r"CBT\d+", category_id):
        result["issues"].append("全球发布必须使用有效的 CBT 末级类目")
        return result
    api = client or MercadoLibreCategoryClient()
    try:
        source = api.category(category_id)
    except Exception as exc:
        result["issues"].append(f"本次未能从官方验证 CBT 类目：{exc}")
        return result
    settings = source.get("settings") or {}
    source_domain = _domain_key(settings.get("catalog_domain") or source.get("domain_id"))
    result.update({"category_name": source.get("name"), "domain": source_domain})
    if settings.get("status") not in (None, "enabled") or settings.get("listing_allowed") is False:
        result["issues"].append("CBT 类目当前不可刊登")
    semantic_issue = _semantic_category_issue(str(payload.get("title") or payload.get("family_name") or ""), source_domain)
    if semantic_issue:
        result["issues"].append(semantic_issue)
    suffix = category_id[3:]
    verified_site_categories = (draft.get("evidence") or {}).get("site_category_checks") or {}
    for site_id in sites:
        check = verified_site_categories.get(site_id) if isinstance(verified_site_categories, dict) else None
        verified_id = str((check or {}).get("category_id") or "").strip().upper() if isinstance(check, dict) else ""
        target_id = verified_id if re.fullmatch(rf"{site_id}\d+", verified_id) else f"{site_id}{suffix}"
        row: dict[str, Any] = {"site_id": site_id, "category_id": target_id, "compatible": False}
        if verified_id:
            row["mapping_source"] = str(check.get("source") or "manual_official_category_check")
            result["warnings"].append(f"{site_id} 使用经官方核验的同域类目作发布前检查；实际目标类目和可售状态以平台发布结果为准")
        try:
            target = api.category(target_id)
            target_settings = target.get("settings") or {}
            target_domain = _domain_key(target_settings.get("catalog_domain") or target.get("domain_id"))
            row.update({"category_name": target.get("name"), "domain": target_domain,
                        "enabled": target_settings.get("status") in (None, "enabled"),
                        "listing_allowed": target_settings.get("listing_allowed") is not False})
            row["compatible"] = bool(row["enabled"] and row["listing_allowed"] and target_domain == source_domain)
            if not row["compatible"]:
                result["issues"].append(f"{site_id} 的官方类目映射不存在、已停用或商品域不一致")
        except Exception as exc:
            row["error"] = str(exc)
            result["issues"].append(f"{site_id} 未取得可用的官方类目映射")
        result["sites"].append(row)
    result["valid"] = not result["issues"] and bool(sites)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PublishError("草稿格式不正确")
    return value


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _attribute(row: dict[str, Any]) -> dict[str, Any] | None:
    attribute_id = str(row.get("id") or "").strip()
    value_id = row.get("value_id")
    value_name = str(row.get("value_name") or "").strip()
    if not attribute_id or (value_id in (None, "") and not value_name):
        return None
    if attribute_id == "ITEM_CONDITION":
        return {"id": attribute_id, "values": [{"id": str(value_id or "2230284"), "name": value_name or "New"}]}
    result: dict[str, Any] = {"id": attribute_id}
    if value_id not in (None, ""):
        result["value_id"] = str(value_id)
    if value_name:
        result["value_name"] = value_name
    return result


def _variation_attribute(row: dict[str, Any]) -> dict[str, Any] | None:
    name = str(row.get("name") or "").strip()
    attribute_id = str(row.get("id") or "").strip()
    value = str(row.get("value") or row.get("value_name") or "").strip()
    value_id = str(row.get("value_id") or "").strip()
    if not value or (not name and not attribute_id):
        return None
    result: dict[str, Any] = {"values": [{}]}
    result["id" if attribute_id else "name"] = attribute_id or name
    if value_id:
        result["values"][0]["id"] = value_id
    result["values"][0]["name"] = value
    return result


def _sku_rows(draft: dict[str, Any]) -> list[dict[str, Any]]:
    payload = draft.get("payload") or {}
    evidence = draft.get("evidence") or {}
    rows = [row for row in evidence.get("sku_details") or [] if isinstance(row, dict)]
    if rows:
        return rows
    return [{
        "sku_id": str(payload.get("seller_sku") or "single"),
        "seller_sku": payload.get("seller_sku"),
        "available_quantity": payload.get("available_quantity"),
        "target_net_proceeds_usd": (draft.get("pricing_plan") or {}).get("target_net_proceeds_usd"),
        "listing_price_usd": (draft.get("pricing_plan") or {}).get("listing_price_usd"),
        "site_pricing": payload.get("sites_to_sell") or [],
        "package": {}, "variation_attributes": [],
    }]


def _gallery_for_sku(draft: dict[str, Any], sku_id: str) -> list[dict[str, Any]]:
    review = ((draft.get("evidence") or {}).get("image_review") or {})
    for row in review.get("sku_listing_galleries") or []:
        if isinstance(row, dict) and str(row.get("sku_id") or "") == sku_id:
            gallery = [item for item in row.get("gallery") or [] if isinstance(item, dict)]
            if gallery:
                return gallery
    if len(_sku_rows(draft)) > 1:
        return []
    return [item for item in review.get("gallery") or [] if isinstance(item, dict)]


def _site_rows(draft: dict[str, Any], sku: dict[str, Any], only_sites: set[str] | None = None) -> list[dict[str, Any]]:
    payload = draft.get("payload") or {}
    multi_sku = len(_sku_rows(draft)) > 1
    source = sku.get("site_pricing") or ([] if multi_sku else payload.get("sites_to_sell") or [])
    result: list[dict[str, Any]] = []
    for raw in source:
        row = raw if isinstance(raw, dict) else {"site_id": raw}
        site_id = str(row.get("site_id") or "").strip().upper()
        if not site_id or (only_sites is not None and site_id not in only_sites):
            continue
        site: dict[str, Any] = {
            "site_id": site_id,
            "logistic_type": str(row.get("logistic_type") or "remote"),
            "listing_type_id": str(row.get("listing_type_id") or "gold_special"),
        }
        price, net = _number(row.get("price")), _number(row.get("net_proceeds"))
        if price is not None and net is not None:
            raise PublishError(f"SKU {sku.get('seller_sku') or sku.get('sku_id')} 的 {site_id} 同时填写了 price 与 net_proceeds")
        if net is None and site["logistic_type"] == "remote" and not multi_sku:
            net = _number(sku.get("target_net_proceeds_usd"))
        if price is None and site["logistic_type"] != "remote" and not multi_sku:
            price = _number(sku.get("listing_price_usd"))
        if net is not None:
            site["net_proceeds"] = net
        elif price is not None:
            site["price"] = price
        result.append(site)
    return result


def validate_publish_skus(draft: dict[str, Any]) -> list[dict[str, Any]]:
    payload = draft.get("payload") or {}
    all_skus = _sku_rows(draft)
    variation_keys = []
    for sku in all_skus:
        variation_keys.append({str(row.get("id") or row.get("name") or "").strip() for row in sku.get("variation_attributes") or [] if isinstance(row, dict)})
    rows: list[dict[str, Any]] = []
    for index, sku in enumerate(all_skus, 1):
        sku_id = str(sku.get("sku_id") or f"sku-{index}")
        seller_sku = str(sku.get("seller_sku") or "").strip()
        issues: list[str] = []
        sites = _site_rows(draft, sku)
        if not seller_sku:
            issues.append("卖家 SKU 为空")
        if not isinstance(sku.get("available_quantity"), (int, float)) or sku.get("available_quantity") <= 0:
            issues.append("库存未填写或不大于 0")
        if not sites:
            issues.append("目标站点为空")
        for site in sites:
            if site["site_id"] == "MLB" and site["logistic_type"] == "fulfillment":
                issues.append("CBT 巴西站不支持 Fulfillment，只能使用 Remote")
            if "price" not in site and "net_proceeds" not in site:
                issues.append(f"{site['site_id']} 缺少独立价格或净回款")
        gallery = _gallery_for_sku(draft, sku_id)
        if not gallery:
            issues.append("SKU 上架图片为空")
        if not payload.get("category_id"):
            issues.append("美客多类目未选择")
        if len(all_skus) > 1 and not variation_keys[index - 1]:
            issues.append("缺少变体组合属性")
        if len(all_skus) > 1 and variation_keys[index - 1] != variation_keys[0]:
            issues.append("变体属性名称与其他 SKU 不一致")
        rows.append({"sku_id": sku_id, "seller_sku": seller_sku, "variant": str(sku.get("variant") or ""),
                     "site_count": len(sites), "image_count": len(gallery), "issues": issues, "valid": not issues})
    return rows


def _package_attributes(sku: dict[str, Any]) -> list[dict[str, Any]]:
    package = sku.get("package") if isinstance(sku.get("package"), dict) else {}
    fields = (("PACKAGE_WEIGHT", "weight_g", "g"), ("PACKAGE_LENGTH", "length_cm", "cm"),
              ("PACKAGE_WIDTH", "width_cm", "cm"), ("PACKAGE_HEIGHT", "height_cm", "cm"))
    return [{"id": attr_id, "value_name": f"{package[key]:g} {unit}"}
            for attr_id, key, unit in fields if isinstance(package.get(key), (int, float)) and package[key] > 0]


def build_user_product_payload(draft: dict[str, Any], picture_ids: list[str] | dict[str, list[str]],
                               only_sites: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    payload = draft.get("payload") or {}
    common = [value for row in payload.get("attributes") or [] if isinstance(row, dict) and (value := _attribute(row))]
    results: list[dict[str, Any]] = []
    for index, sku in enumerate(_sku_rows(draft), 1):
        sku_id = str(sku.get("sku_id") or f"sku-{index}")
        seller_sku = str(sku.get("seller_sku") or payload.get("seller_sku") or "").strip()
        sites = _site_rows(draft, sku, (only_sites or {}).get(seller_sku or sku_id))
        by_key = {str(row.get("id") or row.get("name")): dict(row) for row in common}
        by_key["ITEM_CONDITION"] = {"id": "ITEM_CONDITION", "values": [{"id": str(payload.get("item_condition_value_id") or "2230284"), "name": "New"}]}
        if seller_sku:
            by_key["SELLER_SKU"] = {"id": "SELLER_SKU", "value_name": seller_sku}
        for row in _package_attributes(sku):
            by_key[row["id"]] = row
        for raw in sku.get("variation_attributes") or []:
            if isinstance(raw, dict) and (row := _variation_attribute(raw)):
                by_key[str(row.get("id") or row.get("name"))] = row
        if str(sku.get("gtin") or "").strip():
            by_key["GTIN"] = {"id": "GTIN", "value_name": str(sku["gtin"]).strip()}
        ids = picture_ids.get(sku_id, []) if isinstance(picture_ids, dict) else picture_ids
        item: dict[str, Any] = {
            "sites_to_sell": sites,
            "family_name": str(payload.get("family_name") or payload.get("title") or "").strip(),
            "category_id": str(payload.get("category_id") or "").strip(),
            "currency_id": str(payload.get("currency_id") or "USD"),
            "available_quantity": int(sku.get("available_quantity") or payload.get("available_quantity") or 0),
            "description": {"plain_text": str(payload.get("description") or "").strip()},
            "pictures": [{"id": value} for value in ids],
            "attributes": list(by_key.values()),
        }
        nets = [site["net_proceeds"] for site in sites if "net_proceeds" in site]
        prices = [site["price"] for site in sites if "price" in site]
        if nets and prices:
            raise PublishError(f"SKU {seller_sku or sku_id} 混用了 price 与 net_proceeds，请统一计价模式")
        if nets:
            item["global_net_proceeds"] = _number(sku.get("target_net_proceeds_usd")) or nets[0]
        elif prices:
            item["price"] = _number(sku.get("listing_price_usd")) or prices[0]
        warranty_id, warranty_name = str(payload.get("warranty_type_value_id") or "").strip(), str(payload.get("warranty_type") or "").strip()
        if warranty_id or warranty_name:
            warranty: dict[str, str] = {"id": "WARRANTY_TYPE"}
            if warranty_id: warranty["value_id"] = warranty_id
            if warranty_name: warranty["value_name"] = warranty_name
            item["sale_terms"] = [warranty]
        results.append(item)
    return results


def build_publish_preview(draft: dict[str, Any], category_client: Any | None = None) -> dict[str, Any]:
    validation = validate_publish_skus(draft)
    category_validation = validate_category_sites(draft, category_client)
    if not category_validation["valid"]:
        for row in validation:
            row["issues"].extend(category_validation["issues"])
            row["issues"] = list(dict.fromkeys(row["issues"]))
            row["valid"] = False
    placeholders = {row["sku_id"]: [f"UPLOAD_REQUIRED_{i + 1}" for i in range(row["image_count"])] for row in validation}
    request = build_user_product_payload(draft, placeholders)
    return {"valid": bool(validation) and all(row["valid"] for row in validation), "sku_validation": validation,
            "category_validation": category_validation, "endpoint": "/global/user-products/families", "request": request,
            "note": "UPLOAD_REQUIRED_* 会在真实发布时替换为美客多图片 ID；本预览未上传图片、未发布商品。"}


def _image_bytes(item: dict[str, Any], root: Path) -> tuple[bytes, str, str]:
    local_file = str(item.get("local_file") or "").strip()
    if local_file:
        assets = (root / "assets").resolve()
        path = (assets / local_file).resolve()
        if not path.is_relative_to(assets) or not path.is_file():
            raise PublishError("上架图片本地文件不存在")
        return path.read_bytes(), path.name, mimetypes.guess_type(path.name)[0] or "image/png"
    remote_url = str(item.get("remote_url") or "").strip()
    if not remote_url.startswith("https://"):
        raise PublishError("上架图片地址不正确")
    request = urllib.request.Request(remote_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(10_000_001), Path(urllib.parse.urlparse(remote_url).path).name or "image.jpg", str(response.headers.get_content_type() or "image/jpeg")
    except (urllib.error.URLError, OSError) as exc:
        raise PublishError("无法下载一张上架图片") from exc


def _seller_sku(item: dict[str, Any]) -> str:
    return next((str(row.get("value_name") or "") for row in item.get("attributes") or [] if row.get("id") == "SELLER_SKU"), "")


def _error_text(error: Any) -> str:
    if not isinstance(error, dict): return str(error or "发布失败")
    detail = next((str(row.get("message")) for row in error.get("cause") or [] if isinstance(row, dict) and row.get("message")), "")
    return detail or str(error.get("message") or error.get("error") or "发布失败")


def _attempt_variants(request_payload: list[dict[str, Any]], response: Any) -> list[dict[str, Any]]:
    responses = response if isinstance(response, list) else [response]
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(request_payload):
        result = responses[index] if index < len(responses) and isinstance(responses[index], dict) else {}
        returned = {str(site.get("site_id") or "").upper(): site for site in result.get("site_items") or [] if isinstance(site, dict)}
        sites = []
        for requested in item.get("sites_to_sell") or []:
            site_id, actual = str(requested.get("site_id") or "").upper(), returned.get(str(requested.get("site_id") or "").upper(), {})
            if actual.get("error"): state, error = "failed", _error_text(actual.get("error"))
            elif actual.get("item_id"): state, error = "success", None
            else: state, error = "pending_confirmation", None
            sites.append({"site_id": site_id, "status": state, "item_id": actual.get("item_id"), "error": error})
        rows.append({"seller_sku": _seller_sku(item), "siteless_user_product_id": result.get("siteless_user_product_id"),
                     "siteless_family_id": result.get("siteless_family_id"), "sites": sites})
    return rows


def _record_status(variants: list[dict[str, Any]]) -> tuple[str, str | None]:
    sites = [site for row in variants for site in row.get("sites") or []]
    failed, succeeded = [x for x in sites if x.get("status") == "failed"], [x for x in sites if x.get("status") == "success"]
    error = "；".join(f"{x.get('site_id') or '-'}：{x.get('error') or '发布失败'}" for x in failed) or None
    if any(x.get("status") == "pending_confirmation" for x in sites): return "pending_confirmation", error
    if failed: return ("partial" if succeeded else "failed"), error
    return ("success" if sites else "failed"), error


def _response_outcome(response: Any) -> tuple[str, str | None]:
    results = response if isinstance(response, list) else [response]
    sites = [site for result in results if isinstance(result, dict) for site in result.get("site_items") or [] if isinstance(site, dict)]
    failures, successes = [x for x in sites if x.get("error")], [x for x in sites if x.get("item_id") and not x.get("error")]
    if not failures: return "success", None
    error = "；".join(f"{x.get('site_id') or '-'}：{_error_text(x.get('error'))}" for x in failures)
    return ("partial" if successes else "failed"), error


def _merge_variants(previous: list[dict[str, Any]], latest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = json.loads(json.dumps(previous)) if previous else []
    for row in latest:
        target = next((item for item in merged if item.get("seller_sku") == row.get("seller_sku")), None)
        if target is None: merged.append(row); continue
        target["siteless_user_product_id"] = row.get("siteless_user_product_id") or target.get("siteless_user_product_id")
        target["siteless_family_id"] = row.get("siteless_family_id") or target.get("siteless_family_id")
        for site in row.get("sites") or []:
            old = next((item for item in target.get("sites") or [] if item.get("site_id") == site.get("site_id")), None)
            if old is None: target.setdefault("sites", []).append(site)
            else: old.update(site)
    return merged


def _upload_pictures(draft: dict[str, Any], root: Path, api: MercadoLibreClient, cache: dict[str, str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for index, sku in enumerate(_sku_rows(draft), 1):
        sku_id, ids = str(sku.get("sku_id") or f"sku-{index}"), []
        for image in _gallery_for_sku(draft, sku_id):
            data, filename, content_type = _image_bytes(image, root)
            if len(data) > 10_000_000: raise PublishError("上架图片超过美客多 10 MB 限制")
            fingerprint, picture_id = hashlib.sha256(data).hexdigest(), cache.get(hashlib.sha256(data).hexdigest())
            if not picture_id:
                uploaded = api.upload_picture(data, filename, content_type)
                picture_id = str(uploaded.get("id") or "")
                if not picture_id: raise MercadoLibreAPIError("图片上传成功但未返回图片 ID")
                cache[fingerprint] = picture_id
            ids.append(picture_id)
        result[sku_id] = ids
    return result


def _load_context(name: str, drafts_dir: Path, root: Path, preflight_issues: Any) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    if Path(name).name != name or not name.endswith("-draft.json"): raise PublishError("草稿文件名不正确")
    draft_path = (drafts_dir / name).resolve()
    if not draft_path.is_file() or draft_path.parent != drafts_dir.resolve(): raise PublishError("没有找到草稿")
    approval = root / "reviews" / f"{draft_path.stem}-approval.json"
    if not approval.is_file() or _read_json(approval).get("approved") is not True: raise PublishError("商品尚未通过人工审核")
    draft, issues = _read_json(draft_path), list(preflight_issues(_read_json(draft_path)))
    checks = validate_publish_skus(draft)
    issues.extend(f"SKU {row['seller_sku'] or row['sku_id']}：{issue}" for row in checks for issue in row["issues"])
    if issues: raise PublishError("发布前检查未通过：" + issues[0])
    record_path = root / "data" / "publish_records" / f"{draft_path.stem}.json"
    return draft, record_path, (_read_json(record_path) if record_path.is_file() else {})


def publish_draft(name: str, *, confirmed: bool, drafts_dir: Path, root: Path, preflight_issues: Any,
                  client: MercadoLibreClient | None = None, retry_failed_only: bool = False) -> dict[str, Any]:
    if confirmed is not True: raise PublishError("真实发布需要再次确认")
    draft, record_path, previous = _load_context(name, drafts_dir, root, preflight_issues)
    if previous.get("status") == "success": raise PublishError("该商品已全部发布成功，为避免重复刊登已停止")
    if previous.get("status") == "partial" and not retry_failed_only: raise PublishError("该商品部分站点已成功，请使用‘只重试失败站点’")
    if retry_failed_only and previous.get("status") not in {"partial", "failed"}: raise PublishError("当前记录没有可重试的失败站点")
    api = client or MercadoLibreClient()
    account = api.me()
    if "user_product_seller" not in (account.get("tags") or []): raise PublishError("当前账号未启用 User Products 发布模型")
    if ((account.get("status") or {}).get("sell") or {}).get("allow") is False: raise PublishError("当前美客多账号不允许发布商品")
    category_validation = validate_category_sites(draft, api)
    if not category_validation["valid"]:
        raise PublishError("站点类目校验未通过：" + (category_validation["issues"][0] if category_validation["issues"] else "未知类目错误"))
    cache, attempts = dict(previous.get("upload_cache") or {}), list(previous.get("attempts") or [])
    try:
        full_payload = build_user_product_payload(draft, _upload_pictures(draft, root, api, cache))
        if retry_failed_only:
            failed = {row.get("seller_sku") or "": {x["site_id"] for x in row.get("sites") or [] if x.get("status") == "failed"} for row in previous.get("variants") or []}
            request_payload = [dict(item, sites_to_sell=[x for x in item["sites_to_sell"] if x["site_id"] in failed.get(_seller_sku(item), set())]) for item in full_payload]
            request_payload = [item for item in request_payload if item["sites_to_sell"]]
            if not request_payload: raise PublishError("没有找到可重试的失败站点")
            latest, response = [], []
            prior = {row.get("seller_sku"): row for row in previous.get("variants") or []}
            for item in request_payload:
                up_id = (prior.get(_seller_sku(item)) or {}).get("siteless_user_product_id")
                if not up_id: raise PublishError(f"SKU {_seller_sku(item)} 缺少已创建的 User Product ID，不能安全重试站点")
                one = api.post_json(f"/global/user-products/{up_id}", {"sites_to_sell": item["sites_to_sell"]})
                response.append(one); latest.extend(_attempt_variants([item], one))
            variants, endpoint = _merge_variants(previous.get("variants") or [], latest), "/global/user-products/{siteless_user_product_id}"
        else:
            request_payload, endpoint = full_payload, "/global/user-products/families"
            response = api.post_json(endpoint, request_payload)
            variants = _attempt_variants(request_payload, response)
        status, error = _record_status(variants)
        now = datetime.now(timezone.utc).isoformat()
        attempts.append({"attempted_at": now, "retry_failed_only": retry_failed_only, "endpoint": endpoint,
                         "requested_skus": [_seller_sku(x) for x in request_payload], "response": response})
        record = {"status": status, "draft": name, "published_at": now, "endpoint": endpoint, "upload_cache": cache,
                  "response": response, "variants": variants, "attempts": attempts}
        if error: record["error"] = error
        write_json(record_path, record)
        return record
    except Exception as exc:
        record = dict(previous); record.update({"status": previous.get("status") if previous.get("status") == "partial" else "failed",
            "draft": name, "attempted_at": datetime.now(timezone.utc).isoformat(), "upload_cache": cache, "error": str(exc)[:1000]})
        write_json(record_path, record); raise


def refresh_publish_record(name: str, *, drafts_dir: Path, root: Path,
                           client: MercadoLibreClient | None = None) -> dict[str, Any]:
    if Path(name).name != name or not name.endswith("-draft.json"): raise PublishError("草稿文件名不正确")
    record_path = root / "data" / "publish_records" / f"{Path(name).stem}.json"
    if not record_path.is_file(): raise PublishError("没有发布记录可回查")
    record, api = _read_json(record_path), (client or MercadoLibreClient())
    for variant in record.get("variants") or []:
        up_id = variant.get("siteless_user_product_id")
        if up_id and any(not site.get("item_id") for site in variant.get("sites") or []):
            try:
                up = api.get(f"/user-products/{up_id}")
                listing_sites = up.get("listing_sites") or up.get("sites") or []
                for listing in listing_sites:
                    listing_id = str(listing.get("id") or listing.get("listing_id") or "")
                    site_id = str(listing.get("site_id") or listing_id[:3]).upper()
                    site = next((row for row in variant.get("sites") or [] if row.get("site_id") == site_id), None)
                    if site is None:
                        continue
                    if listing.get("errors") or listing.get("error"):
                        site.update({"status": "failed", "error": _error_text(listing.get("errors") or listing.get("error"))})
                    elif listing_id:
                        site["item_id"] = listing_id
            except MercadoLibreAPIError as exc:
                variant["check_error"] = str(exc)
        for site in variant.get("sites") or []:
            if not site.get("item_id"): continue
            try:
                item = api.get(f"/marketplace/items/{site['item_id']}")
                state = str(item.get("status") or "").lower()
                sub_status = item.get("sub_status") or []
                warnings = item.get("warnings") or []
                cause = item.get("cause") or []
                details = [sub_status, warnings, cause, item.get("status_detail")]
                reason = "；".join(str(value) for value in details if value not in (None, "", [], {}))
                if state == "active":
                    outcome = "success"
                elif state == "paused" and not reason:
                    outcome = "success"
                elif state in {"paused", "inactive", "closed"} and reason:
                    outcome = "failed"
                else:
                    outcome = "pending_confirmation"
                update = {"marketplace_status": state or "unknown", "status": outcome,
                          "category_id": item.get("category_id"), "domain_id": item.get("domain_id"),
                          "checked_at": datetime.now(timezone.utc).isoformat()}
                if reason:
                    update["error"] = reason[:1000]
                site.update(update)
            except MercadoLibreAPIError as exc:
                site.update({"status": "pending_confirmation", "check_error": str(exc)})
    status, error = _record_status(record.get("variants") or [])
    record.update({"status": status, "checked_at": datetime.now(timezone.utc).isoformat()})
    if error: record["error"] = error
    else: record.pop("error", None)
    write_json(record_path, record)
    return record
