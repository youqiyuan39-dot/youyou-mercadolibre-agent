"""Platform-neutral product evidence and adapters for the additive v0.3 workbench."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Protocol


def clean_description(text: str) -> str:
    """Keep raw capture elsewhere; discard trailing marketplace legal boilerplate."""
    return re.split(r"【平台活动下价格】|【非平台活动下价格】|内容声明：|价格说明", str(text or ""), maxsplit=1)[0].strip()


def product_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    evidence, payload = draft.get("evidence") or {}, draft.get("payload") or {}
    skus = copy.deepcopy(evidence.get("sku_details") or (evidence.get("supplier_page") or {}).get("skus") or [])
    selected_id = str(evidence.get("current_pricing_sku_id") or "")
    selected = next((s for s in skus if str(s.get("sku_id") or s.get("id")) == selected_id), None)
    gallery = evidence.get("image_review") or {}
    supplier = evidence.get("supplier_page") or {}
    facts = {
        "title": supplier.get("title") or payload.get("family_name") or payload.get("title") or "",
        "description": clean_description(supplier.get("description") or ""),
        "source_url": evidence.get("supplier_url") or "",
        "skus": skus, "selected_sku_id": selected_id, "selected_sku": selected,
        "images": {key: copy.deepcopy(gallery.get(key) or []) for key in ("source_gallery", "detail_gallery", "sku_source_galleries")},
        "packaging": {"weight": copy.deepcopy(evidence.get("packed_weight")), "dimensions": copy.deepcopy(evidence.get("packed_dimensions"))},
        "purchase_cost": copy.deepcopy(evidence.get("purchase_cost") or ({"value": selected.get("purchase_cost_cny"), "currency": "CNY", "source": "SELECTED_SUPPLIER_SKU"} if selected and selected.get("purchase_cost_cny") is not None else None)),
        "locks": copy.deepcopy((draft.get("edit_metadata") or {}).get("field_locks") or {}),
    }
    digest = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    content_version = hashlib.sha256(json.dumps({"payload": payload, "template": evidence.get("prompt_template")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"schema_version": 1, "product_id": str(evidence.get("source_product_id") or digest[:16]), "facts": facts, "facts_version": digest, "content_version": content_version}


def _capacities(text: str) -> set[str]:
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*(ml|毫升|litros?|litres?|liters?|l|升)(?![a-zA-Z])", str(text), re.I)
    return {str(float(number.replace(',', '.')) * (1 if unit.lower() in {'ml', '毫升'} else 1000)) for number, unit in matches}


def review_issues(draft: dict[str, Any], proposal: dict[str, Any] | None = None) -> list[str]:
    """Semantic checks, independent of any marketplace's field-presence checks."""
    product = product_from_draft(draft)
    selected = product["facts"]["selected_sku"]
    evidence, payload = draft.get("evidence") or {}, draft.get("payload") or {}
    output = proposal if proposal is not None else payload
    issues = []
    if product["facts"]["selected_sku_id"] and selected is None:
        issues.append("当前 SKU 不在采集规格中，需重新选择")
    if selected:
        expected = _capacities(selected.get("variant") or selected.get("name") or "")
        fields = [("标题", output.get("title")), ("描述", output.get("description"))]
        fields.extend(("站点标题", s.get("title")) for s in output.get("site_titles") or output.get("sites_to_sell") or [] if isinstance(s, dict))
        attrs = output.get("attributes") or []
        fields.extend(("容量属性", a.get("value_name")) for a in attrs if isinstance(a, dict) and "CAPACITY" in str(a.get("id") or ""))
        for label, text in fields:
            actual = _capacities(text or "")
            if expected and actual and not actual.issubset(expected):
                issues.append(f"{label}容量与当前 SKU 不一致：当前 {','.join(sorted(expected))}ml，产物 {','.join(sorted(actual))}ml")
    attributes = {a.get("id"): a for a in payload.get("attributes") or [] if isinstance(a, dict)}
    gtin_row = attributes.get("GTIN") or attributes.get("EAN") or attributes.get("UPC") or {}
    gtin = evidence.get("gtin") or gtin_row.get("value_name") or payload.get("gtin")
    provenance = str(evidence.get("gtin_source") or evidence.get("barcode_source") or gtin_row.get("source") or "").lower()
    if gtin and (any(marker in provenance for marker in ("generated", "auto", "ai_mapping", "attribute_model")) or not evidence.get("gtin")):
        issues.append("条码缺少可核验来源或为自动生成；校验位正确不能证明条码真实")
    if evidence.get("gtin_or_exemption") == "NO_GTIN_PENDING_CATEGORY_CHECK":
        issues.append("无条码原因仍待官方类目确认，不能视为已取得豁免依据")
    for label, value in (("重量", evidence.get("packed_weight") or {}), ("尺寸", evidence.get("packed_dimensions") or {})):
        status = str(value.get("status") or "").upper()
        if any(token in status for token in ("ESTIMATE", "DEFAULT", "UNVERIFIED", "AI_")):
            issues.append(f"包装{label}为估算、默认或未确认值，需人工核实")
    return list(dict.fromkeys(issues))


class MarketplaceAdapter(Protocol):
    platform: str
    def preview(self, product: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]: ...


class MercadoLibreAdapter:
    platform = "mercadolibre"
    def preview(self, product: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        # Platform-specific conversion remains in the verified existing converter.
        from .publish_service import build_publish_preview
        return {"platform": self.platform, "store_id": target.get("store_id"), "facts_version": product["facts_version"], "preview": build_publish_preview(copy.deepcopy(target["draft"]))}


class OfflineSecondPlatformAdapter:
    """Exercises replacement boundaries only; does not pretend to implement a real API."""
    platform = "offline_contract_test"
    def preview(self, product: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        return {"platform": self.platform, "mode": "OFFLINE_CONTRACT_ONLY", "facts_version": product["facts_version"], "title": product["facts"]["title"][:int(target.get("title_limit") or 80)], "variants": copy.deepcopy(product["facts"]["skus"]), "store_id": target.get("store_id")}


def target_identity(product: dict[str, Any], platform: str, store_id: str, market: str) -> str:
    if not all((platform, store_id, market)):
        raise ValueError("发布目标必须明确平台、店铺和市场")
    return hashlib.sha256(json.dumps([product["product_id"], product["facts_version"], product.get("content_version"), platform, store_id, market]).encode()).hexdigest()
