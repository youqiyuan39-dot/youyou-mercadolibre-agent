"""Normalize and store supplier evidence received from the local collector."""

from __future__ import annotations

import json
import hashlib
import copy
import re
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs


ROOT = Path(__file__).resolve().parents[1]
VERSION = "SOURCE_INTAKE_20261018_SINGLE_SKU_V2"
INTAKE_DIR = ROOT / "data" / "intake"
DRAFTS_DIR = ROOT / "drafts"
FORBIDDEN_KEYS = {
    "cookie", "cookies", "authorization", "password", "passwd", "token",
    "access_token", "refresh_token", "api_key", "apikey", "client_secret",
    "localstorage", "sessionstorage",
}


class IntakeError(ValueError):
    pass


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if normalized in FORBIDDEN_KEYS or _contains_forbidden_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _text(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None

def _package_number(value):
    result=_number(value)
    return result if result is not None and result>0 else None


def _image_urls(values: Any, limit: int = 30) -> list[str]:
    result: list[str] = []
    for item in values or []:
        url = _text(item.get("url") if isinstance(item, dict) else item, 1000)
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname and url not in result:
            result.append(url)
        if len(result) >= limit:
            break
    return result


def _public_value(value: Any, depth: int = 0) -> Any:
    """Bound optional public supplier metadata without retaining page internals."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:50]:
            clean_key = _text(key, 80)
            clean_child = _public_value(child, depth + 1)
            if clean_key and clean_child not in (None, "", [], {}):
                result[clean_key] = clean_child
        return result
    if isinstance(value, list):
        return [item for item in (_public_value(child, depth + 1) for child in value[:50]) if item not in (None, "", [], {})]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return _text(value, 500)


def _sku_image_evidence(values: Any) -> list[dict[str, Any]]:
    """Keep public labelled thumbnails even when their SKU cannot yet be resolved."""
    result, seen = [], set()
    for row in values[:200] if isinstance(values, list) else []:
        if not isinstance(row, dict):
            continue
        owner = {key: _text(row.get(key), 120 if key in {'sku_id', 'spec_id'} else 300)
                 for key in ('sku_id', 'spec_id', 'variant', 'label')}
        if not any(owner.values()):
            continue
        urls = _image_urls(row.get('images') or [row.get('url')], 8)
        urls = [url for url in urls if any((urlparse(url).hostname or '').lower() == domain or
                (urlparse(url).hostname or '').lower().endswith('.' + domain)
                for domain in ('alicdn.com', 'tbcdn.cn', 'tbcdn.com', '1688.com'))]
        if not urls:
            continue
        value = {**{k: v for k, v in owner.items() if v}, 'images': urls,
                 'source_method': _text(row.get('source_method') or 'VISIBLE_SKU_LABEL', 80)}
        if row.get('captured_at'):
            value['captured_at'] = _text(row['captured_at'], 80)
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if signature not in seen:
            result.append(value)
            seen.add(signature)
    return result


def normalize_collection(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IntakeError("采集结果必须是 JSON 对象")
    if _contains_forbidden_key(payload):
        raise IntakeError("采集结果包含禁止保存的登录或密钥字段")

    source_url = _text(payload.get("source_url"), 1000)
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.hostname != "detail.1688.com":
        raise IntakeError("当前只接收 detail.1688.com 商品详情页")
    offer_match = re.search(r"/offer/(\d+)\.html", parsed.path)
    if not offer_match:
        raise IntakeError("无法识别 1688 商品编号")

    # Public handoff identifiers only; never retain arbitrary URL fragments.
    handoff = parse_qs(parsed.fragment)
    market_id = (handoff.get("youyou_market") or [""])[0]
    task_id = (handoff.get("youyou_task") or [""])[0]
    references = []
    if re.fullmatch(r"ML[MB]\d{6,20}", market_id):
        reference = {"market_item_id": market_id, "match_status": "SPEC_MATCH_UNVERIFIED"}
        if re.fullmatch(r"[a-f0-9]{32}", task_id):
            reference["discovery_task_id"] = task_id
        references.append(reference)
    source_url = f"https://detail.1688.com/offer/{offer_match.group(1)}.html"

    title = _text(payload.get("title"), 300)
    if not title:
        raise IntakeError("没有采集到商品标题")

    pricing = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else {}
    from .market_follow_v3 import reference
    try: market_reference=reference(payload.get('market_reference'))
    except (ValueError, TypeError): market_reference=None
    package = payload.get("package") if isinstance(payload.get("package"), dict) else {}
    shipping = payload.get("domestic_shipping") if isinstance(payload.get("domestic_shipping"), dict) else {}
    collection_mode = _text(payload.get("collection_mode"), 20).lower()
    if collection_mode not in {"single", "multi"}:
        collection_mode = "single"
    package_input_mode = _text(payload.get("package_input_mode"), 20).lower()
    if package_input_mode not in {"manual", "ai_auto"}:
        package_input_mode = "manual" if package.get("evidence") == "USER_INPUT_UNVERIFIED" else "ai_auto"
    product_images = _image_urls(payload.get("product_images") or payload.get("images"), 30)
    detail_images = _image_urls(payload.get("detail_images"), 40)
    # New collectors return a complete gallery per source SKU. Keep the old
    # single-image list as a compatibility projection for existing drafts.
    raw_sets = payload.get("sku_image_sets") or payload.get("sku_image_groups") or []
    if not raw_sets:
        raw_sets = [item for item in (payload.get("sku_images") or []) if isinstance(item, dict)]
    sku_image_sets: list[dict[str, Any]] = []
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        image_values = item.get("images") or item.get("image_urls") or item.get("urls") or [item]
        # Global and SKU galleries may intentionally reference the same photo.
        # Deduplication must not erase its SKU binding.
        urls = _image_urls(image_values, 8)
        if not _text(item.get('sku_id'),120):
            # An unbound DOM thumbnail is not SKU evidence simply because it
            # appears in the public gallery. Explicit mappings are retained.
            urls=[url for url in urls if url not in set(product_images)]
        if not urls:
            continue
        sku_id = _text(item.get("sku_id"), 120)
        variant = _text(item.get("variant"), 300)
        duplicate = next((row for row in sku_image_sets if row["sku_id"] == sku_id and row["variant"] == variant), None)
        if duplicate:
            duplicate["images"] = _image_urls([*duplicate["images"], *urls], 8)
        else:
            sku_image_sets.append({"sku_id": sku_id, "variant": variant, "images": urls})
        if len(sku_image_sets) >= 100:
            break
    sku_images = [
        {"url": row["images"][0], "variant": row["variant"][:80], "sku_id": row["sku_id"]}
        for row in sku_image_sets
    ]

    skus: list[dict[str, Any]] = []
    for item in payload.get("skus") or []:
        if not isinstance(item, dict):
            continue
        pack = item.get("package") if isinstance(item.get("package"), dict) else {}
        skus.append({
            "sku_id": _text(item.get("sku_id"), 120),
            "spec_id": _text(item.get("spec_id"), 120),
            "variant": _text(item.get("variant"), 300),
            "price": _number(item.get("price")),
            "inventory": _number(item.get("inventory")),
            "package": {
                "weight_g": _package_number(pack.get("weight_g")),
                "length_cm": _package_number(pack.get("length_cm")),
                "width_cm": _package_number(pack.get("width_cm")),
                "height_cm": _package_number(pack.get("height_cm")),
                "evidence": _text(pack.get('evidence'),120),
                "status": _text(pack.get('status'),80),
                "source_label": _text(pack.get('source_label'),300),
            } if pack else None,
        })
        if len(skus) >= 500:
            break

    selected_sku_id = _text(payload.get("selected_sku_id"), 120)
    if selected_sku_id:
        if collection_mode != "single" or sum(row["sku_id"] == selected_sku_id for row in skus) != 1:
            raise IntakeError("单SKU选择与本次真实规格不一致，请重新选择；未创建任务")

    attributes: dict[str, str] = {}
    for key, value in (payload.get("attributes") or {}).items() if isinstance(payload.get("attributes"), dict) else []:
        clean_key = _text(key, 80)
        clean_value = _text(value, max(300, len(str(value or ''))))
        if clean_key and clean_value:
            attributes[clean_key] = clean_value
        if len(attributes) >= 80:
            break

    return {
        "status": "COLLECTED_UNVERIFIED",
        "source": "1688",
        "source_url": source_url,
        "api_evidence": copy.deepcopy(payload.get('api_evidence')) if isinstance(payload.get('api_evidence'),dict) else None,
        "offer_id": offer_match.group(1),
        "market_references": references,
        "market_reference": market_reference,
        "title": title,
        "collection_mode": collection_mode,
        "selected_sku_id": selected_sku_id,
        "package_input_mode": package_input_mode,
        "selected_variant": [_text(item, 120) for item in (payload.get("selected_variant") or []) if _text(item, 120)][:12],
        "description": _text(payload.get("description"), max(10000, len(str(payload.get("description") or '')))),
        "minimum_order_quantity": _number(payload.get("minimum_order_quantity")),
        "pricing_tiers": [
            {"min_quantity": _number(row.get("min_quantity")), "price": _number(row.get("price"))}
            for row in (payload.get("pricing_tiers") or []) if isinstance(row, dict)
        ][:30],
        "skus": skus,
        "pricing": {
            "amount": _number(pricing.get("amount")),
            "currency": "CNY",
            "evidence": _text(pricing.get("evidence"), 120) or "visible_page",
        },
        "images": product_images,
        "product_images": product_images,
        "sku_images": sku_images,
        "sku_image_sets": sku_image_sets,
        "sku_image_evidence": _sku_image_evidence(payload.get('sku_image_evidence')),
        "package_rows":[{'sku_id':_text(row.get('sku_id'),120),'spec_id':_text(row.get('spec_id'),120),'variant':_text(row.get('variant'),300),
            'package':{key:_package_number(row['package'].get(key)) for key in ('weight_g','length_cm','width_cm','height_cm')},
            **{key:_public_value(row[key]) for key in ('headers','values','table_title','quoted_text','source_method','row_index','dimension_type','weight_type') if key in row}}
            for row in (payload.get('package_rows') or [])[:500] if isinstance(row,dict) and isinstance(row.get('package'),dict)],
        "unassigned_package_rows":_public_value(payload.get('unassigned_package_rows') or []),
        "capture_diagnostics":_public_value(payload.get('capture_diagnostics') or {}),
        "detail_images": detail_images,
        "image_counts": {
            "product": len(product_images),
            "sku": sum(len(row["images"]) for row in sku_image_sets),
            "detail": len(detail_images),
        },
        "package": {
            "weight_g": _package_number(package.get("weight_g")),
            "length_cm": _package_number(package.get("length_cm")),
            "width_cm": _package_number(package.get("width_cm")),
            "height_cm": _package_number(package.get("height_cm")),
            "evidence": _text(package.get("evidence"), 120),
        },
        "domestic_shipping": {
            "amount": 0 if shipping.get('free') is True else _number(shipping.get("amount")),
            "currency": "CNY",
            "free": shipping.get("free") is True,
            "evidence": _text(shipping.get("evidence"), 120),
        },
        "attributes": attributes,
        "unit": _text(payload.get("unit"), 50),
        "category": _public_value(payload.get("category") or {}),
        "sales": _public_value(payload.get("sales") or {}),
        "offer_flags": _public_value(payload.get("offer_flags") or {}),
        "cross_border": _public_value(payload.get("cross_border") or {}),
        "guarantees": [_text(value, 160) for value in (payload.get("guarantees") or []) if _text(value, 160)][:30],
        "buyer_protection": _public_value(payload.get("buyer_protection") or []),
        "seller": {
            "company_name": _text((payload.get("seller") or {}).get("company_name"), 300),
            "login_id": _text((payload.get("seller") or {}).get("login_id"), 160),
            "member_id": _text((payload.get("seller") or {}).get("member_id"), 160),
            "auth_company_name": _text((payload.get("seller") or {}).get("auth_company_name"), 300),
            "shop_url": _text((payload.get("seller") or {}).get("shop_url"), 1000),
            "seller_type": _text((payload.get("seller") or {}).get("seller_type"), 80),
            "service_score": _number((payload.get("seller") or {}).get("service_score")),
            "buyer_repeat_rate": _text((payload.get("seller") or {}).get("buyer_repeat_rate"), 80),
        } if isinstance(payload.get("seller"), dict) else {},
        "extraction_source": _text(payload.get("extraction_source"), 80) or "visible_dom_fallback",
        "description_url": _text(payload.get("description_url"), 1000),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "cookies_collected": False,
            "browser_storage_collected": False,
            "raw_html_collected": False,
        },
    }


def save_collection(payload: dict[str, Any], directory: Path = INTAKE_DIR) -> Path:
    normalized = normalize_collection(payload)
    return _save_normalized_collection(normalized, directory)


def _save_normalized_collection(normalized: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = directory / f"1688-{normalized['offer_id']}-{timestamp}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def build_processing_draft(normalized: dict[str, Any], intake_path: Path, root: Path = ROOT) -> dict[str, Any]:
    """Create a safe local draft from visible supplier facts."""

    from .packaging_table_v3 import materialize
    normalized=materialize(normalized)

    price = normalized["pricing"].get("amount")
    package = normalized["package"]
    package_is_manual = normalized.get("package_input_mode") == "manual"
    package_status = "USER_INPUT_UNVERIFIED" if package_is_manual else "SUPPLIER_PACKAGING_INFO"
    product_rows = [
        {
            "role": "main" if index == 0 else f"source_{index + 1}",
            "remote_url": url,
            "image_type": "product",
            "review_status": "UNREVIEWED",
        }
        for index, url in enumerate(normalized.get("product_images") or normalized.get("images") or [])
    ]
    sku_source_galleries = []
    sku_rows = []
    for sku_index, source_set in enumerate(normalized.get("sku_image_sets") or []):
        sku_id = source_set.get("sku_id") or ""
        variant = source_set.get("variant") or ""
        gallery = []
        for image_index, url in enumerate(source_set.get("images") or []):
            row = {
                "role": "main" if image_index == 0 else f"sku_{sku_index + 1}_{image_index + 1}",
                "remote_url": url,
                "image_type": "sku",
                "variant": variant,
                "sku_id": sku_id,
                "review_status": "UNREVIEWED",
            }
            gallery.append(row)
            sku_rows.append(row)
        if gallery:
            sku_source_galleries.append({
                "sku_id": sku_id,
                "variant": variant,
                "gallery": gallery,
                "review_status": "UNREVIEWED",
            })
    source_rows = product_rows + [
        row for row in sku_rows
        if row["remote_url"] not in {item["remote_url"] for item in product_rows}
    ]
    detail_rows = [
        {
            "role": f"detail_{index + 1}",
            "remote_url": url,
            "image_type": "detail",
            "review_status": "EVIDENCE_ONLY",
        }
        for index, url in enumerate(normalized.get("detail_images") or [])
    ]
    seller_sku = "AUTO-" + hashlib.sha1(normalized["offer_id"].encode("utf-8")).hexdigest()[:8]
    source_skus = normalized.get("skus") or []
    sku_details = []
    for index, row in enumerate(source_skus):
        if not isinstance(row, dict):
            continue
        pack = row.get("package") if isinstance(row.get("package"), dict) else {}
        sku_id = str(row.get("sku_id") or f"source-{index + 1}")
        image_set = next((item for item in normalized.get("sku_image_sets") or [] if str(item.get("sku_id") or "") == sku_id), None)
        sku_details.append({
            "sku_id": sku_id,
            "sku_id_origin": row.get('sku_id_origin') or 'SUPPLIER_SKU',
            "spec_id": str(row.get("spec_id") or ""),
            "variant": str(row.get("variant") or ""),
            "seller_sku": f"{seller_sku}-{index + 1:03d}",
            "purchase_cost_cny": row.get("price") if row.get("price") is not None else (price if len(source_skus) == 1 else None),
            "listing_price_usd": None,
            "target_net_proceeds_usd": None,
            "site_pricing": [],
            "available_quantity": 200,
            "inventory_source": "DEFAULT_200_USER_POLICY",
            "supplier_inventory_observed": row.get("inventory"),
            "package": {
                "weight_g": pack.get("weight_g") if pack.get("weight_g") is not None else (package.get("weight_g") if len(source_skus) == 1 else None),
                "length_cm": pack.get("length_cm") if pack.get("length_cm") is not None else (package.get("length_cm") if len(source_skus) == 1 else None),
                "width_cm": pack.get("width_cm") if pack.get("width_cm") is not None else (package.get("width_cm") if len(source_skus) == 1 else None),
                "height_cm": pack.get("height_cm") if pack.get("height_cm") is not None else (package.get("height_cm") if len(source_skus) == 1 else None),
                "status": package_status,
                "field_sources": copy.deepcopy(pack.get('field_sources') or {}),
                "field_status": copy.deepcopy(pack.get('field_status') or {}),
            },
            "barcode_type": "NO_GTIN",
            "gtin": "",
            "variation_attributes": [{"name": "货源规格", "value": str(row.get("variant") or "")}],
            "source_image_count": len((image_set or {}).get("images") or []),
        })
    selected_id = normalized.get("selected_sku_id")
    unselected_sku_details = []
    if selected_id:
        unselected_sku_details = [row for row in sku_details if row["sku_id"] != selected_id]
        sku_details = [row for row in sku_details if row["sku_id"] == selected_id]
        if len(sku_details) != 1:
            raise IntakeError("无法唯一定位本次选择的SKU；未创建任务")
        price = sku_details[0].get("purchase_cost_cny")
        if not package_is_manual:
            package = copy.deepcopy(sku_details[0].get("package") or {})
    selected_sku = sku_details[0] if sku_details else None
    ref=normalized.get('market_reference')
    if ref and len(sku_details)==1:
        selected_sku['market_reference']=copy.deepcopy(ref)
        selected_sku['site_pricing']=[{'site_id':ref['site_id'],'logistic_type':'remote','listing_type_id':'gold_special','net_proceeds':None,'price':None}]
    available_quantity = 200
    inventory_source = "DEFAULT_200_USER_POLICY"
    return {
        "status": "DRAFT_LOCAL_ONLY",
        "queue_status": "PENDING_PROCESSING",
        "requested_action": "draft",
        "publish_endpoint": "/global/user-products",
        "model": "user_products",
        "payload": {
            "title": normalized["title"],
            "family_name": normalized["title"],
            "description": "",
            "category_id": None,
            "currency_id": "USD",
            "available_quantity": available_quantity,
            "seller_sku": selected_sku["seller_sku"] if selected_sku else seller_sku,
            "barcode_type": "NO_GTIN",
            "buying_mode": "buy_it_now",
            "condition": "new",
            "item_condition_value_id": "2230284",
            "catalog_listing": False,
            "warranty_type": "No warranty",
            "warranty_type_value_id": "6150835",
            "warranty_time": "",
            "pictures": [],
            "attributes": [],
            "sites_to_sell": copy.deepcopy((selected_sku or {}).get('site_pricing') or []),
        },
        "evidence": {
            "package_table_resolution":copy.deepcopy(normalized.get('package_table_resolution') or {}),
            "classification_review": {"status":"NOT_RUN","boundary":"1688分类不是美客多类目；须按商品用途、配件身份和官方完整路径匹配"},
            "market_reference": copy.deepcopy(ref),
            "source_title": normalized["title"],
            "source_capture": _relative_to_root(intake_path, root),
            "source_product_id": normalized["offer_id"],
            "supplier_url": normalized["source_url"],
            "market_references": copy.deepcopy(normalized.get("market_references") or []),
            "purchase_cost": (
                {
                    "amount": price,
                    "currency": "CNY",
                    "status": "VISIBLE_SELECTED_PRICE_UNVERIFIED",
                }
                if price is not None
                else None
            ),
            "packed_weight": (
                {"value": package["weight_g"], "unit": "g", "status": package_status}
                if package.get("weight_g") is not None else None
            ),
            "packed_dimensions": (
                {
                    "length": package["length_cm"], "width": package["width_cm"],
                    "height": package["height_cm"], "unit": "cm", "status": package_status,
                }
                if all(package.get(key) is not None for key in ("length_cm", "width_cm", "height_cm")) else None
            ),
            "supplier_page": {
                "captured_at": normalized["captured_at"],
                "selected_variant": [selected_sku["variant"]] if selected_id else normalized.get("selected_variant") or [],
                "collection_mode": normalized.get("collection_mode") or "single",
                "package_input_mode": normalized.get("package_input_mode") or "ai_auto",
                "product_weight_g": package.get("weight_g"),
                "product_length_cm": package.get("length_cm"),
                "product_width_cm": package.get("width_cm"),
                "product_height_cm": package.get("height_cm"),
                "package_table_evidence": package.get("evidence"),
                "package_rows": normalized.get('package_rows') or [],
                "capture_diagnostics": normalized.get('capture_diagnostics') or {},
                "domestic_shipping": normalized.get("domestic_shipping"),
                "attributes": normalized.get("attributes") or {},
                "unit": normalized.get("unit") or "",
                "category": normalized.get("category") or {},
                "sales": normalized.get("sales") or {},
                "offer_flags": normalized.get("offer_flags") or {},
                "cross_border": normalized.get("cross_border") or {},
                "guarantees": normalized.get("guarantees") or [],
                "buyer_protection": normalized.get("buyer_protection") or [],
                "description": normalized.get("description") or "",
                "minimum_order_quantity": normalized.get("minimum_order_quantity"),
                "pricing_tiers": normalized.get("pricing_tiers") or [],
                "skus": normalized.get("skus") or [],
                "sku_details": sku_details,
                "current_pricing_sku_id": selected_sku["sku_id"] if selected_sku else "",
                "sku_image_sets": normalized.get("sku_image_sets") or [],
                "available_quantity": available_quantity,
                "inventory_source": inventory_source,
                "seller": normalized.get("seller") or {},
                "extraction_source": normalized.get("extraction_source"),
                "description_url": normalized.get("description_url"),
            },
            "image_review": {
                "source_gallery": source_rows,
                "sku_gallery": sku_rows,
                "sku_source_galleries": sku_source_galleries,
                "detail_gallery": detail_rows,
                # A source photo is not a listing selection until screened or chosen.
                "gallery": [],
                "counts": normalized.get("image_counts") or {},
            },
            "sku_details": sku_details,
            "unselected_sku_details": unselected_sku_details,
            "processing_scope": {"mode": "single" if selected_id else normalized.get("collection_mode"),
                                 "selected_sku_id": selected_id or "",
                                 "source": "USER_SELECTED_CAPTURED_SKU" if selected_id else "LEGACY_CAPTURE"},
            "current_pricing_sku_id": selected_sku["sku_id"] if selected_sku else "",
            "images_human_reviewed": False,
            "gtin_or_exemption": "NO_GTIN_PENDING_CATEGORY_CHECK",
            "compatibility_evidence": "",
            "package_ai": {
                "status": "SKIPPED_MANUAL" if package_is_manual else "NOT_NEEDED_SUPPLIER_EVIDENCE" if all(package.get(k) for k in ('weight_g','length_cm','width_cm','height_cm')) else "PENDING",
                "human_review_required": True,
            },
        },
        "human_approved": False,
        "pricing_plan": {
            "mode": "net_proceeds",
            "purchase_cost_cny": price,
            "domestic_shipping_cny": normalized.get("domestic_shipping", {}).get("amount"),
            "status": "REAL_PROFIT_UNVERIFIED",
        },
    }


def _source_managed(value: Any, old_title: str, old_company: str) -> bool:
    """Whether a title still belongs to the collector and may be refreshed."""
    current = _text(value, 300)
    return not current or current == old_title or current == old_company


def refresh_draft_from_collection(
    draft: dict[str, Any], normalized: dict[str, Any], intake_path: Path, root: Path = ROOT
) -> dict[str, Any]:
    """Refresh supplier facts while preserving fields that a person has edited.

    Earlier versions preserved the whole first draft. That also preserved failed
    captures forever. Source evidence is now refreshed on every collection;
    explicit field locks and human-reviewed image choices remain untouched.
    """
    if not isinstance(draft.get("payload"), dict) or not isinstance(draft.get("evidence"), dict):
        return draft

    prior_capture = root / str(draft['evidence'].get('source_capture') or '')
    if prior_capture.is_file() and prior_capture.resolve().is_relative_to(root.resolve()):
        try:
            previous = json.loads(prior_capture.read_text(encoding='utf-8'))
            from .auto_processing_v3 import capture_facts as facts
            if facts(previous) == facts(normalized) and draft['evidence'].get('processing_automation'):
                return copy.deepcopy(draft)
        except (OSError, ValueError):
            pass

    fresh = build_processing_draft(normalized, intake_path, root)
    updated = copy.deepcopy(draft)
    payload = updated["payload"]
    evidence = updated["evidence"]
    old_supplier = evidence.get("supplier_page") if isinstance(evidence.get("supplier_page"), dict) else {}
    old_skus={str(s.get('sku_id')):s for s in old_supplier.get('skus') or []}
    capture_locks=(updated.get('edit_metadata') or {}).get('field_locks') or {}
    current_skus=evidence.setdefault('sku_details',[])
    # A scope change changes the active work, never deletes previous human work.
    if normalized.get('selected_sku_id') or normalized.get('collection_mode') == 'multi':
        known = {str(row.get('sku_id')) for row in current_skus}
        for archived in evidence.get('unselected_sku_details') or []:
            if str(archived.get('sku_id')) not in known:
                current_skus.append(copy.deepcopy(archived))
                known.add(str(archived.get('sku_id')))
    for incoming in fresh['evidence']['sku_details']:
        current=next((s for s in current_skus if str(s.get('sku_id'))==str(incoming['sku_id'])),None)
        if current is None:
            current_skus.append(copy.deepcopy(incoming));continue
        old=old_skus.get(str(incoming['sku_id']),{})
        old_price=old.get('price')
        if old_price is None and len(fresh['evidence']['sku_details'])==1:
            old_price=(evidence.get('purchase_cost') or {}).get('amount')
        cost_status=str(current.get('purchase_cost_status') or '').upper()
        if 'USER' not in cost_status and 'MANUAL' not in cost_status and (current.get('purchase_cost_cny') is None or current.get('purchase_cost_cny')==old_price) and incoming.get('purchase_cost_cny') is not None:
            current['purchase_cost_cny']=incoming['purchase_cost_cny']
        pack=current.setdefault('package',{}) or {};current['package']=pack
        status=str(pack.get('status') or '').upper()
        old_pack=old.get('package') or {}
        for key in ('weight_g','length_cm','width_cm','height_cm'):
            lock='weight' if key=='weight_g' else 'dimensions'
            if (current.get('human_field_locks') or {}).get(lock) or str(current.get('sku_id'))==str(evidence.get('current_pricing_sku_id')) and capture_locks.get(lock):continue
            new=(incoming.get('package') or {}).get(key)
            if new is not None and not any(s in status for s in ('USER','MANUAL','MEASURED','HUMAN','VERIFIED')) and (pack.get(key) is None or pack.get(key)==old_pack.get(key)):
                pack[key]=new;pack['status']=(incoming.get('package') or {}).get('status')
                for meta in ('field_sources','field_status'):
                    if key in (incoming.get('package',{}).get(meta) or {}):pack.setdefault(meta,{})[key]=copy.deepcopy(incoming['package'][meta][key])
        prior_reference=current.get('market_reference') or {}
        locked_match=prior_reference.get('spec_match') in {'HUMAN_CONFIRMED_SAME_SKU','CONFIRMED_SAME_SKU'} or (current.get('procurement') or {}).get('locked') is True
        if incoming.get('market_reference') and not locked_match:current['market_reference']=copy.deepcopy(incoming['market_reference'])
        if not current.get('site_pricing') and incoming.get('site_pricing'):current['site_pricing']=copy.deepcopy(incoming['site_pricing'])
    selected_id = normalized.get('selected_sku_id')
    if selected_id:
        selected = [row for row in current_skus if str(row.get('sku_id')) == selected_id]
        if len(selected) != 1:
            raise IntakeError('无法唯一定位所选SKU；已保留原稿')
        evidence['unselected_sku_details'] = [row for row in current_skus if str(row.get('sku_id')) != selected_id]
        current_skus[:] = selected
        evidence['current_pricing_sku_id'] = selected_id
        evidence['processing_scope'] = copy.deepcopy(fresh['evidence']['processing_scope'])
    elif normalized.get('collection_mode') == 'multi':
        evidence['unselected_sku_details'] = []
        evidence['processing_scope'] = copy.deepcopy(fresh['evidence']['processing_scope'])
    old_title = ""
    old_capture = root / str(evidence.get("source_capture") or "")
    if old_capture.is_file():
        try:
            old_title = _text(json.loads(old_capture.read_text(encoding="utf-8")).get("title"), 300)
        except (OSError, json.JSONDecodeError):
            old_title = ""
    old_company = _text((old_supplier.get("seller") or {}).get("company_name"), 300)
    locks = ((updated.get("edit_metadata") or {}).get("field_locks") or {})
    title_locked = locks.get("title") is True or locks.get("family_name") is True
    if not title_locked and _source_managed(payload.get("title"), old_title, old_company):
        payload["title"] = normalized["title"]
    if not title_locked and _source_managed(payload.get("family_name"), old_title, old_company):
        payload["family_name"] = normalized["title"]

    for key, value in fresh["payload"].items():
        payload.setdefault(key, copy.deepcopy(value))
    if payload.get("available_quantity") is None or old_supplier.get("inventory_source") in {
        "1688_SKU_AVAILABLE_QUANTITY", "DEFAULT_1000_WHEN_1688_UNAVAILABLE"
    }:
        payload["available_quantity"] = fresh["payload"]["available_quantity"]
    for sku in evidence.get("sku_details") or []:
        if not isinstance(sku, dict):
            continue
        if sku.get("inventory_source") in {
            "1688_SKU_AVAILABLE_QUANTITY", "EXISTING_SUPPLIER_CAPTURE", "DEFAULT_1000_WHEN_1688_UNAVAILABLE"
        }:
            sku.setdefault("supplier_inventory_observed", sku.get("available_quantity"))
            sku["available_quantity"] = 200
            sku["inventory_source"] = "DEFAULT_200_USER_POLICY"

    evidence["source_capture"] = _relative_to_root(intake_path, root)
    evidence["source_product_id"] = normalized["offer_id"]
    evidence["supplier_url"] = normalized["source_url"]
    if normalized.get('market_reference'):
        evidence['market_reference']=copy.deepcopy(normalized['market_reference'])
        if not payload.get('sites_to_sell'):payload['sites_to_sell']=copy.deepcopy(fresh['payload']['sites_to_sell'])
    references = evidence.setdefault("market_references", [])
    for reference in normalized.get("market_references") or []:
        if reference not in references:
            references.append(copy.deepcopy(reference))

    purchase_status = str((evidence.get("purchase_cost") or {}).get("status") or "").upper()
    if "USER" not in purchase_status and "MANUAL" not in purchase_status:
        evidence["purchase_cost"] = copy.deepcopy(fresh["evidence"]["purchase_cost"])

    for field in ("packed_weight", "packed_dimensions"):
        current_status = str((evidence.get(field) or {}).get("status") or "").upper()
        if not capture_locks.get('weight' if field=='packed_weight' else 'dimensions') and not any(s in current_status for s in ('USER','MANUAL','MEASURED','HUMAN','VERIFIED')):
            evidence[field] = copy.deepcopy(fresh["evidence"][field])

    evidence["supplier_page"] = copy.deepcopy(fresh["evidence"]["supplier_page"])
    current_review = evidence.get("image_review") if isinstance(evidence.get("image_review"), dict) else {}
    fresh_review = fresh["evidence"]["image_review"]
    keep_listing = evidence.get("images_human_reviewed") is True or locks.get("pictures") is True
    listing_gallery = copy.deepcopy(current_review.get("gallery") or []) if keep_listing else copy.deepcopy(fresh_review["gallery"])
    evidence["image_review"] = copy.deepcopy(fresh_review)
    evidence["image_review"]["gallery"] = listing_gallery
    for key in ('sku_listing_galleries', 'source_screening', 'generated_gallery'):
        if key in current_review: evidence['image_review'][key] = copy.deepcopy(current_review[key])
    evidence.setdefault("images_human_reviewed", False)
    if not evidence.get("gtin_or_exemption"):
        evidence["gtin_or_exemption"] = "NO_GTIN_PENDING_CATEGORY_CHECK"
    payload.setdefault("barcode_type", "NO_GTIN")
    if not evidence.get("gtin") and evidence.get("gtin_or_exemption") == "NO_GTIN_PENDING_CATEGORY_CHECK":
        payload["barcode_type"] = "NO_GTIN"
        payload["catalog_listing"] = False

    complete_package = fresh["evidence"].get("packed_weight") and fresh["evidence"].get("packed_dimensions")
    evidence["package_ai"] = {
        "status": "NOT_NEEDED_SUPPLIER_EVIDENCE" if complete_package else "PENDING",
        "human_review_required": True,
    }
    pricing = updated.setdefault("pricing_plan", {})
    pricing_status = str(pricing.get("purchase_cost_status") or "").upper()
    if "USER" not in pricing_status and "MANUAL" not in pricing_status:
        pricing["purchase_cost_cny"] = fresh["pricing_plan"].get("purchase_cost_cny")
        pricing["domestic_shipping_cny"] = normalized.get("domestic_shipping", {}).get("amount")
    pricing.setdefault("mode", "net_proceeds")
    pricing.setdefault("status", "REAL_PROFIT_UNVERIFIED")
    updated["queue_status"] = "PENDING_PROCESSING"
    updated["status"] = "DRAFT_LOCAL_ONLY"
    return updated


def save_collection_and_enqueue(payload: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """Save raw intake, create a draft once, and rebuild the local board."""

    normalized = normalize_collection(payload)
    intake_path = _save_normalized_collection(normalized, root / "data" / "intake")
    draft_path = root / "drafts" / f"1688-{normalized['offer_id']}-draft.json"
    draft_created = not draft_path.exists()
    if draft_created:
        draft = build_processing_draft(normalized, intake_path, root)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = draft_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(draft_path)
    else:
        existing = json.loads(draft_path.read_text(encoding="utf-8"))
        refreshed = refresh_draft_from_collection(existing, normalized, intake_path, root)
        if refreshed != existing:
            temporary = draft_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(draft_path)

    materialized=build_processing_draft(normalized,intake_path,root)
    package_complete=bool(materialized['evidence'].get('packed_weight') and materialized['evidence'].get('packed_dimensions'))
    package_ai_status = "NOT_NEEDED_SUPPLIER_EVIDENCE" if package_complete else "NOT_NEEDED_MANUAL"
    if normalized.get("package_input_mode") == "ai_auto" and not package_complete:
        try:
            from .package_ai_pipeline import prepare_package_request
        except ImportError:
            from package_ai_pipeline import prepare_package_request
        prepare_package_request(draft_path, root)
        package_ai_status = "REQUEST_PREPARED"

    try:
        from .workflow_orchestrator import build_board, write_json
    except ImportError:
        from workflow_orchestrator import build_board, write_json

    board = build_board(root / "drafts", root)
    board_path = root / "data" / "workflow_board.json"
    write_json(board_path, board)
    queue_item = next((item for item in board["items"] if Path(item["draft"]) == draft_path), None)
    from .auto_processing_v3 import enqueue
    auto_processing = enqueue(root, draft_path, normalized)
    return {
        "intake": intake_path,
        "draft": draft_path,
        "board": board_path,
        "draft_created": draft_created,
        "queue_status": "PENDING_PROCESSING",
        "validation_stage": queue_item["stage"] if queue_item else "UNKNOWN",
        "next_action": queue_item["next_action"] if queue_item else "打开任务板检查",
        "image_counts": normalized.get("image_counts") or {},
        "package_ai_status": package_ai_status,
        "auto_processing": auto_processing,
    }
