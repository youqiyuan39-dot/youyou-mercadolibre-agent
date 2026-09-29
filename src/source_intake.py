"""Normalize and store supplier evidence received from the local collector."""

from __future__ import annotations

import json
import hashlib
import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
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
    return result if result >= 0 else None


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

    title = _text(payload.get("title"), 300)
    if not title:
        raise IntakeError("没有采集到商品标题")

    pricing = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else {}
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
        urls = [url for url in _image_urls(image_values, 8) if url not in set(product_images)]
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
                "weight_g": _number(pack.get("weight_g")),
                "length_cm": _number(pack.get("length_cm")),
                "width_cm": _number(pack.get("width_cm")),
                "height_cm": _number(pack.get("height_cm")),
            } if pack else None,
        })
        if len(skus) >= 500:
            break

    attributes: dict[str, str] = {}
    for key, value in (payload.get("attributes") or {}).items() if isinstance(payload.get("attributes"), dict) else []:
        clean_key = _text(key, 80)
        clean_value = _text(value, 300)
        if clean_key and clean_value:
            attributes[clean_key] = clean_value
        if len(attributes) >= 80:
            break

    return {
        "status": "COLLECTED_UNVERIFIED",
        "source": "1688",
        "source_url": source_url,
        "offer_id": offer_match.group(1),
        "title": title,
        "collection_mode": collection_mode,
        "package_input_mode": package_input_mode,
        "selected_variant": [_text(item, 120) for item in (payload.get("selected_variant") or []) if _text(item, 120)][:12],
        "description": _text(payload.get("description"), 10000),
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
        "detail_images": detail_images,
        "image_counts": {
            "product": len(product_images),
            "sku": sum(len(row["images"]) for row in sku_image_sets),
            "detail": len(detail_images),
        },
        "package": {
            "weight_g": _number(package.get("weight_g")),
            "length_cm": _number(package.get("length_cm")),
            "width_cm": _number(package.get("width_cm")),
            "height_cm": _number(package.get("height_cm")),
            "evidence": _text(package.get("evidence"), 120),
        },
        "domestic_shipping": {
            "amount": _number(shipping.get("amount")),
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
            "spec_id": str(row.get("spec_id") or ""),
            "variant": str(row.get("variant") or ""),
            "seller_sku": f"{seller_sku}-{index + 1:03d}",
            "purchase_cost_cny": row.get("price") if row.get("price") is not None else price,
            "listing_price_usd": None,
            "target_net_proceeds_usd": None,
            "site_pricing": [],
            "available_quantity": int(row["inventory"]) if row.get("inventory") is not None else 1000,
            "inventory_source": "1688_SKU_AVAILABLE_QUANTITY" if row.get("inventory") is not None else "DEFAULT_1000_WHEN_1688_UNAVAILABLE",
            "package": {
                "weight_g": pack.get("weight_g") if pack.get("weight_g") is not None else package.get("weight_g"),
                "length_cm": pack.get("length_cm") if pack.get("length_cm") is not None else package.get("length_cm"),
                "width_cm": pack.get("width_cm") if pack.get("width_cm") is not None else package.get("width_cm"),
                "height_cm": pack.get("height_cm") if pack.get("height_cm") is not None else package.get("height_cm"),
            },
            "barcode_type": "NO_GTIN",
            "gtin": "",
            "variation_attributes": [{"name": "货源规格", "value": str(row.get("variant") or "")}],
            "source_image_count": len((image_set or {}).get("images") or []),
        })
    selected_sku = sku_details[0] if sku_details else None
    supplier_inventories = [int(row["inventory"]) for row in source_skus if row.get("inventory") is not None]
    available_quantity = selected_sku["available_quantity"] if selected_sku else (sum(supplier_inventories) if supplier_inventories else 1000)
    inventory_source = selected_sku["inventory_source"] if selected_sku else ("1688_SKU_AVAILABLE_QUANTITY" if supplier_inventories else "DEFAULT_1000_WHEN_1688_UNAVAILABLE")
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
            "sites_to_sell": [],
        },
        "evidence": {
            "source_capture": _relative_to_root(intake_path, root),
            "source_product_id": normalized["offer_id"],
            "supplier_url": normalized["source_url"],
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
                "selected_variant": normalized.get("selected_variant") or [],
                "collection_mode": normalized.get("collection_mode") or "single",
                "package_input_mode": normalized.get("package_input_mode") or "ai_auto",
                "product_weight_g": package.get("weight_g"),
                "product_length_cm": package.get("length_cm"),
                "product_width_cm": package.get("width_cm"),
                "product_height_cm": package.get("height_cm"),
                "package_table_evidence": package.get("evidence"),
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
                # Keep every source image, but preselect only the first three
                # product images for the listing review area.
                "gallery": [dict(item) for item in product_rows[:3]],
                "counts": normalized.get("image_counts") or {},
            },
            "sku_details": sku_details,
            "current_pricing_sku_id": selected_sku["sku_id"] if selected_sku else "",
            "images_human_reviewed": False,
            "gtin_or_exemption": "NO_GTIN_PENDING_CATEGORY_CHECK",
            "compatibility_evidence": "",
            "package_ai": {
                "status": "SKIPPED_MANUAL" if package_is_manual else "PENDING",
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

    fresh = build_processing_draft(normalized, intake_path, root)
    updated = copy.deepcopy(draft)
    payload = updated["payload"]
    evidence = updated["evidence"]
    old_supplier = evidence.get("supplier_page") if isinstance(evidence.get("supplier_page"), dict) else {}
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

    evidence["source_capture"] = _relative_to_root(intake_path, root)
    evidence["source_product_id"] = normalized["offer_id"]
    evidence["supplier_url"] = normalized["source_url"]

    purchase_status = str((evidence.get("purchase_cost") or {}).get("status") or "").upper()
    if "USER" not in purchase_status and "MANUAL" not in purchase_status:
        evidence["purchase_cost"] = copy.deepcopy(fresh["evidence"]["purchase_cost"])

    for field in ("packed_weight", "packed_dimensions"):
        current_status = str((evidence.get(field) or {}).get("status") or "").upper()
        if "USER" not in current_status and "MANUAL" not in current_status:
            evidence[field] = copy.deepcopy(fresh["evidence"][field])

    evidence["supplier_page"] = copy.deepcopy(fresh["evidence"]["supplier_page"])
    current_review = evidence.get("image_review") if isinstance(evidence.get("image_review"), dict) else {}
    fresh_review = fresh["evidence"]["image_review"]
    keep_listing = evidence.get("images_human_reviewed") is True or locks.get("pictures") is True
    listing_gallery = copy.deepcopy(current_review.get("gallery") or []) if keep_listing else copy.deepcopy(fresh_review["gallery"])
    evidence["image_review"] = copy.deepcopy(fresh_review)
    evidence["image_review"]["gallery"] = listing_gallery
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
        pricing["purchase_cost_cny"] = normalized["pricing"].get("amount")
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

    package_complete = normalized["package"].get("weight_g") is not None and all(
        normalized["package"].get(key) is not None for key in ("length_cm", "width_cm", "height_cm")
    )
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
    queue_item = next((item for item in board["items"] if (root / item["draft"]).resolve() == draft_path.resolve()), None)
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
    }
