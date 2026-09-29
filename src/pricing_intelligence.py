"""Read-only competitor evidence and bounded contribution-profit guidance."""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from .mercadolibre_client import MercadoLibreAPIError, MercadoLibreClient
from .exchange_rates import ExchangeRateError, fetch_exchange_rate


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def apply_inventory_rule(draft: dict[str, Any]) -> None:
    payload = draft.setdefault("payload", {})
    supplier = draft.setdefault("evidence", {}).setdefault("supplier_page", {})
    if _number(payload.get("available_quantity")) is not None:
        return
    inventories = [int(value) for row in supplier.get("skus") or [] if (value := _number(row.get("inventory"))) is not None]
    payload["available_quantity"] = sum(inventories) if inventories else 1000
    supplier["available_quantity"] = payload["available_quantity"]
    supplier["inventory_source"] = "1688_SKU_AVAILABLE_QUANTITY" if inventories else "DEFAULT_1000_WHEN_1688_UNAVAILABLE"


def forwarder_unit_cost(pricing: dict[str, Any]) -> dict[str, Any]:
    """Calculate confirmed Xinbiao warehouse/packing charges for one order unit."""
    service = _number(pricing.get("forwarder_packing_service_cny"))
    material = _number(pricing.get("forwarder_packaging_material_cny"))
    value_added = _number(pricing.get("forwarder_value_added_cny"))
    label = _number(pricing.get("forwarder_label_cny"))
    service = 0.6 if service is None else service
    material = 0.0 if material is None else material
    value_added = 0.0 if value_added is None else value_added
    label = (0.1 if pricing.get("forwarder_stocking") is True else 0.0) if label is None else label
    weight_g = _number(pricing.get("package_weight_g"))
    length = _number(pricing.get("package_length_cm"))
    width = _number(pricing.get("package_width_cm"))
    height = _number(pricing.get("package_height_cm"))
    oversize = any(value is not None and value > 45 for value in (length, width, height))
    actual_kg = weight_g / 1000 if weight_g is not None else None
    volume_kg = length * width * height / 12000 if oversize and None not in (length, width, height) else None
    weights = [value for value in (actual_kg, volume_kg) if value is not None]
    billable_kg = max(weights) if weights else None
    overweight = max(0.0, billable_kg - 5.0) if oversize and billable_kg is not None else 0.0
    total = service + material + value_added + label + overweight
    return {
        "source": "鑫标服务收费明细26.8.15(1).doc",
        "packing_service_cny": round(service, 2),
        "packaging_material_cny": round(material, 2),
        "value_added_service_cny": round(value_added, 2),
        "warehouse_label_cny": round(label, 2),
        "oversize_rule_applied": oversize,
        "volumetric_divisor_if_any_side_over_45cm": 12000,
        "billable_weight_kg": round(billable_kg, 3) if billable_kg is not None else None,
        "over_5kg_surcharge_cny": round(overweight, 2),
        "total_cny": round(total, 2),
    }


def profit_methodology(pricing: dict[str, Any]) -> dict[str, Any]:
    mode = str(pricing.get("mode") or "net_proceeds").strip().lower()
    required = {
        "purchase_cost_cny": _number(pricing.get("purchase_cost_cny")),
        "domestic_shipping_cny": _number(pricing.get("domestic_shipping_cny")),
        "exchange_rate_cny_per_usd": _number(pricing.get("exchange_rate_cny_per_usd")),
        "packaging_cost_usd": _number(pricing.get("packaging_cost_usd")),
    }
    other_cost = _number(pricing.get("other_cost_usd"))
    if mode != "net_proceeds":
        required["cross_border_freight_usd"] = _number(pricing.get("cross_border_freight_usd"))
    missing = [key for key, value in required.items() if value is None or (key == "exchange_rate_cny_per_usd" and value <= 0)]
    forwarder = forwarder_unit_cost(pricing)
    result: dict[str, Any] = {
        "objective": "unit_contribution_profit",
        "acceptable_margin_range_pct": [10, 30],
        "preferred_margin_pct": 30,
        "pricing_mode": mode,
        "formula": (
            "单位贡献利润=卖家净回款-采购-国内运费-包装-货代操作费-其他变动成本；"
            "Remote 的平台佣金和跨境物流由美客多加到买家售价，不从 net_proceeds 重复扣除；"
            "贡献利润率=单位贡献利润/卖家净回款"
            if mode == "net_proceeds" else
            "单位贡献利润=卖家净回款-采购-国内运费-包装-货代操作费-跨境运费-其他变动成本；贡献利润率=单位贡献利润/卖家净回款"
        ),
        "missing_inputs": missing,
        "forwarder_cost": forwarder,
    }
    if missing:
        result["status"] = "REAL_PROFIT_UNVERIFIED"
        return result
    base_usd = (required["purchase_cost_cny"] + required["domestic_shipping_cny"]) / required["exchange_rate_cny_per_usd"]
    forwarder_usd = forwarder["total_cny"] / required["exchange_rate_cny_per_usd"]
    total_cost = base_usd + required["packaging_cost_usd"] + forwarder_usd + (other_cost or 0)
    if mode != "net_proceeds":
        total_cost += required["cross_border_freight_usd"]
    low = total_cost / 0.90
    high = total_cost / 0.70
    result.update({
        "status": "PROVISIONAL_CONTRIBUTION_RANGE",
        "known_variable_cost_usd": round(total_cost, 2),
        "forwarder_cost_usd": round(forwarder_usd, 2),
        "recommended_net_proceeds_range_usd": [round(low, 2), round(high, 2)],
        "unit_contribution_range_usd": [round(low - total_cost, 2), round(high - total_cost, 2)],
        "preferred_target_net_proceeds_usd": round(high, 2),
        "limitations": "该区间以卖家净回款为口径；税费、广告、退货等若未计入其他成本，仍不能称为最终净利润。货代包装材料和增值服务未选择时按 0 计算。",
        "assumptions": (["其他变动成本未填写，当前暂按 0 USD 估算"] if other_cost is None else []),
        "platform_cost_treatment": (
            "REMOTE_NET_PROCEEDS_INCLUDES_PLATFORM_SHIPPING_AND_SALE_FEE"
            if mode == "net_proceeds" else "MANUAL_PRICE_REQUIRES_PLATFORM_COST_INPUTS"
        ),
    })
    target = _number(pricing.get("target_net_proceeds_usd"))
    if target and target > 0:
        margin = (target - total_cost) / target * 100
        result["current_target"] = {
            "net_proceeds_usd": round(target, 2),
            "unit_contribution_usd": round(target - total_cost, 2),
            "margin_pct": round(margin, 1),
            "assessment": (
                "NEAR_30_PERCENT_TARGET" if 29 <= margin <= 31
                else ("ACCEPTABLE" if 10 <= margin < 29 else ("BELOW_ACCEPTABLE" if margin < 10 else "ABOVE_PREFERRED"))
            ),
        }
    return result


def shipping_methodology(draft: dict[str, Any]) -> dict[str, Any]:
    evidence = draft.get("evidence") or {}
    payload = draft.get("payload") or {}
    pricing_mode = str((draft.get("pricing_plan") or {}).get("mode") or "net_proceeds").strip().lower()
    weight = _number((evidence.get("packed_weight") or {}).get("value"))
    dimensions = evidence.get("packed_dimensions") or {}
    length, width, height = (_number(dimensions.get(key)) for key in ("length", "width", "height"))
    actual_kg = weight / 1000 if weight is not None else None
    volumetric_kg = length * width * height / 6000 if None not in (length, width, height) else None
    billable = max(value for value in (actual_kg, volumetric_kg) if value is not None) if any(value is not None for value in (actual_kg, volumetric_kg)) else None
    site_rows = []
    for raw in payload.get("sites_to_sell") or []:
        row = raw if isinstance(raw, dict) else {"site_id": raw}
        cost = _number(row.get("shipping_cost_usd"))
        site_rows.append({
            "site_id": row.get("site_id"), "shipping_cost_usd": cost,
            "status": (
                "PLATFORM_INCLUDED_IN_LISTING_PRICE"
                if pricing_mode == "net_proceeds" and str(row.get("logistic_type") or "remote") == "remote"
                else ("USER_OR_OFFICIAL_QUOTE_SAVED" if cost is not None else "OFFICIAL_SHIPPING_QUOTE_PENDING")
            ),
        })
    return {
        "actual_weight_kg": round(actual_kg, 3) if actual_kg is not None else None,
        "volumetric_weight_kg": round(volumetric_kg, 3) if volumetric_kg is not None else None,
        "billable_weight_kg": round(billable, 3) if billable is not None else None,
        "internal_volumetric_divisor": 6000,
        "forwarder_oversize_rule": {
            "trigger": "任一边超过 45 cm",
            "volumetric_divisor": 12000,
            "billable_weight": "实重与体积重取大值",
            "over_5kg_surcharge_cny_per_kg": 1,
        },
        "sites": site_rows,
        "note": (
            "Remote + net_proceeds 时，美客多按站点自动把平台佣金和跨境物流加入买家售价；运费仅作售价拆解信息，不从卖家净回款重复扣除。"
            if pricing_mode == "net_proceeds" else
            "使用 price 定价时，巴西和墨西哥必须按站点、物流方式、毛重和尺寸取得官方报价。"
        ),
    }


def collect_competitor_pricing(draft: dict[str, Any], client: MercadoLibreClient | None = None, limit: int = 20, rate_provider=fetch_exchange_rate) -> dict[str, Any]:
    payload = draft.get("payload") or {}
    pricing = draft.get("pricing_plan") or {}
    sites = payload.get("sites_to_sell") or []
    normalized_sites = [row.get("site_id") if isinstance(row, dict) else row for row in sites]
    query = str(payload.get("title") or payload.get("family_name") or "").strip()
    exchange_rates: dict[str, Any] = {}
    if _number(pricing.get("purchase_cost_cny")) is not None and not _number(pricing.get("exchange_rate_cny_per_usd")):
        try:
            cny = rate_provider("CNY", "USD")
            pricing["exchange_rate_cny_per_usd"] = round(1 / float(cny["rate"]), 6)
            pricing["exchange_rate_source"] = cny
            exchange_rates["CNY_USD"] = cny
        except ExchangeRateError as exc:
            exchange_rates["CNY_USD"] = {"error": str(exc)}
    base = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "Mercado Libre official read-only search API",
        "query": query,
        "sites": [],
        "exchange_rates": exchange_rates,
        "profit_guidance": profit_methodology(pricing),
        "shipping_guidance": shipping_methodology(draft),
    }
    if not normalized_sites:
        return {**base, "status": "NO_TARGET_SITES", "error": "尚未选择目标站点，未查询竞品。"}
    if not query:
        return {**base, "status": "NO_QUERY", "error": "没有可用的商品标题，未查询竞品。"}
    try:
        api = client or MercadoLibreClient()
        for site in normalized_sites:
            response = api.search_competitors(str(site), query, limit)
            rows = []
            for item in response.get("results") or []:
                price = _number(item.get("price"))
                if price is None or str(item.get("condition") or "new") != "new":
                    continue
                rows.append({
                    "id": item.get("id"), "title": item.get("title"), "price": price,
                    "currency_id": item.get("currency_id"), "condition": item.get("condition"),
                    "free_shipping": (item.get("shipping") or {}).get("free_shipping"),
                    "permalink": item.get("permalink"),
                })
            prices = [row["price"] for row in rows]
            source_row = next((raw for raw in sites if (raw.get("site_id") if isinstance(raw, dict) else raw) == site), {})
            site_pricing = dict(pricing)
            if isinstance(source_row, dict) and _number(source_row.get("shipping_cost_usd")) is not None:
                site_pricing["cross_border_freight_usd"] = _number(source_row.get("shipping_cost_usd"))
            site_result = {"site_id": site, "sample_count": len(rows), "items": rows[:10], "profit_guidance": profit_methodology(site_pricing)}
            if prices:
                site_result["price_band_local"] = {
                    "currency_id": rows[0].get("currency_id"),
                    "p25": round(_percentile(prices, .25), 2),
                    "median": round(statistics.median(prices), 2),
                    "p75": round(_percentile(prices, .75), 2),
                }
                currency = str(rows[0].get("currency_id") or "USD")
                try:
                    fx = rate_provider(currency, "USD")
                    exchange_rates[f"{currency}_USD"] = fx
                    usd_prices = [value * float(fx["rate"]) for value in prices]
                    site_result["price_band_usd"] = {
                        "p25": round(_percentile(usd_prices, .25), 2),
                        "median": round(statistics.median(usd_prices), 2),
                        "p75": round(_percentile(usd_prices, .75), 2),
                    }
                except ExchangeRateError as exc:
                    exchange_rates[f"{currency}_USD"] = {"error": str(exc)}
            base["sites"].append(site_result)
        base["status"] = "COMPETITOR_DATA_READY" if any(row["sample_count"] for row in base["sites"]) else "NO_COMPARABLE_RESULTS"
        return base
    except MercadoLibreAPIError as exc:
        return {**base, "status": "COMPETITOR_DATA_UNAVAILABLE", "error": str(exc)}
