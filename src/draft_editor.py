"""Local-only product detail editor for Mercado Libre drafts.

The editor never publishes. Paid image generation only starts after the user
presses the explicit image-generation button. Every normal save backs up the
working draft, writes the edited values back to it, and keeps a timestamped
snapshot under ``draft_versions``.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import json
import mimetypes
import re
import sys
import time
from threading import Thread
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

try:
    from .mercadolibre_client import MercadoLibreClient, MercadoLibreCategoryClient, MercadoLibreAPIError, category_preflight, write_json
    from .source_intake import IntakeError, save_collection_and_enqueue
    from .validate_listing import validate
    from .workflow_orchestrator import publish_status_from_record, workflow_item
    from .image_generation import build_image_prompts, build_sku_image_prompts, resolve_market_language, run_job
    from .model_settings import ModelSettingsError, get_public_settings, get_runtime_role, save_model_settings, test_connection
    from .ai_listing_pipeline import AIListingError, _output_text, build_responses_request, call_responses_api, generate_quality_checked_proposal
    from .pricing_intelligence import apply_inventory_rule, collect_competitor_pricing, profit_methodology
    from .store_management import authorization_url, complete_authorization, save_oauth_configuration, save_store_preferences, set_active_store, store_status
    from .publish_service import PublishError, build_publish_preview, publish_draft, refresh_publish_record, select_category_candidate, validate_publish_skus
except ImportError:
    from mercadolibre_client import MercadoLibreClient, MercadoLibreCategoryClient, MercadoLibreAPIError, category_preflight, write_json
    from source_intake import IntakeError, save_collection_and_enqueue
    from validate_listing import validate
    from workflow_orchestrator import publish_status_from_record, workflow_item
    from image_generation import build_image_prompts, build_sku_image_prompts, resolve_market_language, run_job
    from model_settings import ModelSettingsError, get_public_settings, get_runtime_role, save_model_settings, test_connection
    from ai_listing_pipeline import AIListingError, _output_text, build_responses_request, call_responses_api, generate_quality_checked_proposal
    from pricing_intelligence import apply_inventory_rule, collect_competitor_pricing, profit_methodology
    from store_management import authorization_url, complete_authorization, save_oauth_configuration, save_store_preferences, set_active_store, store_status
    from publish_service import PublishError, build_publish_preview, publish_draft, refresh_publish_record, select_category_candidate, validate_publish_skus


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
DRAFTS_DIR = ROOT / "drafts"
VERSIONS_DIR = ROOT / "draft_versions"
ASSETS_DIR = ROOT / "assets"
YOUYOU_EXTENSION_ORIGIN = "chrome-extension://gjjohgdhpecbgodfcdmdnjedajbjakge"
SAFE_DRAFT_NAME = re.compile(r"^[A-Za-z0-9._-]+-draft\.json$")
SAFE_CATEGORY_ID = re.compile(r"^[A-Z]{2,4}\d{2,20}$")
FORBIDDEN_KEYS = {
    "cookie", "cookies", "authorization", "password", "passwd", "token",
    "access_token", "refresh_token", "api_key", "apikey", "client_secret",
    "localstorage", "sessionstorage",
}
IMAGE_HOST = re.compile(r"(^|\.)(alicdn\.com|tbcdn\.cn|1688\.com)$", re.I)


class EditorError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EditorError("草稿格式不正确")
    return value


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if normalized in FORBIDDEN_KEYS or _contains_forbidden_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _draft_path(name: str, drafts_dir: Path = DRAFTS_DIR) -> Path:
    clean = unquote(name)
    if not SAFE_DRAFT_NAME.fullmatch(clean):
        raise EditorError("草稿文件名不正确")
    path = drafts_dir / clean
    if not path.is_file():
        raise EditorError("没有找到草稿")
    return path


def publish_preflight_issues(draft: dict[str, Any]) -> list[str]:
    payload = draft.get("payload") or {}
    evidence = draft.get("evidence") or {}
    pricing = draft.get("pricing_plan") or {}
    issues: list[str] = []
    sites = payload.get("sites_to_sell") or []
    if not sites:
        issues.append("目标站点未保存")
    quantity = payload.get("available_quantity")
    if not isinstance(quantity, (int, float)) or quantity <= 0:
        issues.append("库存未填写或不大于 0")
    if not payload.get("category_id"):
        issues.append("美客多类目未选择")
    if not (((evidence.get("image_review") or {}).get("gallery")) or payload.get("pictures")):
        issues.append("上架图片为空")
    global_amount = pricing.get("target_net_proceeds_usd")
    for raw in sites:
        row = raw if isinstance(raw, dict) else {"site_id": raw}
        if row.get("price") is None and row.get("net_proceeds") is None and global_amount is None:
            issues.append(f"站点 {row.get('site_id') or '-'} 缺少价格或净收益")
    for sku in validate_publish_skus(draft):
        issues.extend(f"SKU {sku['seller_sku'] or sku['sku_id']}：{message}" for message in sku["issues"])
    return issues


def list_drafts(drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(drafts_dir.glob("*-draft.json")):
        draft = read_json(path)
        try:
            item = workflow_item(path, root)
        except Exception as exc:  # Keep one broken draft from hiding the list.
            item = {
                "stage": "READ_ERROR",
                "next_action": "检查草稿格式",
                "errors": [str(exc)],
                "warnings": [],
            }
        payload = draft.get("payload") or {}
        evidence = draft.get("evidence") or {}
        image_review = evidence.get("image_review") or {}
        source_gallery = image_review.get("source_gallery") or image_review.get("gallery") or []
        source_id = str(evidence.get("source_product_id") or "").strip()
        latest_capture = None
        if source_id.isdigit():
            captures = sorted((root / "data" / "intake").glob(f"1688-{source_id}-*.json"))
            if captures:
                latest_capture = read_json(captures[-1])
        latest_images = (latest_capture or {}).get("product_images") or (latest_capture or {}).get("images") or []
        thumbnail = ""
        if latest_images:
            thumbnail = latest_images[0]
        elif source_gallery and isinstance(source_gallery[0], dict):
            thumbnail = source_gallery[0].get("remote_url") or source_gallery[0].get("local_file") or ""
        display_errors = list(item.get("errors") or [])
        latest_package = (latest_capture or {}).get("package") or {}
        if latest_package.get("weight_g") is not None:
            display_errors = [value for value in display_errors if "打包重量" not in value]
        if all(latest_package.get(key) is not None for key in ("length_cm", "width_cm", "height_cm")):
            display_errors = [value for value in display_errors if "打包尺寸" not in value]
        if evidence.get("supplier_url") and payload.get("catalog_listing") is not True:
            display_errors = [value for value in display_errors if "真实条码" not in value]
        display_stage = "CAPTURE_READY" if item["stage"] == "BLOCKED_EVIDENCE" and not display_errors else item["stage"]
        weight = evidence.get("packed_weight") or {}
        dimensions = evidence.get("packed_dimensions") or {}
        pricing = draft.get("pricing_plan") or {}
        image_job_path = root / "jobs" / f"{path.stem}-image-job.json"
        image_job = read_json(image_job_path) if image_job_path.is_file() else None
        generated_count = sum(1 for row in (image_job or {}).get("items") or [] if row.get("status") == "done")
        review_path = root / "reviews" / f"{path.stem}-approval.json"
        review_record = read_json(review_path) if review_path.is_file() else None
        publish_record_path = root / "data" / "publish_records" / f"{path.stem}.json"
        publish_record = read_json(publish_record_path) if publish_record_path.is_file() else {}
        publish_status, publish_error = publish_status_from_record(publish_record)
        publish_issues = publish_preflight_issues(draft)
        proposal_path = root / "ai_outputs" / f"{path.stem}-proposal.json"
        proposal_record = read_json(proposal_path) if proposal_path.is_file() else {}
        listing_title = (proposal_record.get("proposal") or proposal_record).get("title") or payload.get("title")
        if review_record and review_record.get("approved") is True and publish_status == "pending":
            display_stage = "READY_TO_PUBLISH"
        rows.append(
            {
                "name": path.name,
                "title": (latest_capture or {}).get("title") or payload.get("family_name") or payload.get("title") or path.stem,
                "listing_title": listing_title,
                "category_id": payload.get("category_id"),
                "stage": display_stage,
                "next_action": item["next_action"],
                "errors": display_errors,
                "warnings": item.get("warnings") or [],
                "source_url": evidence.get("supplier_url") or "",
                "thumbnail": thumbnail,
                "image_count": len(latest_images) if latest_images else len(source_gallery),
                "generated_image_count": generated_count,
                "image_job_status": (image_job or {}).get("status"),
                "image_job_error": (image_job or {}).get("error"),
                "full_ai_status": item.get("full_ai_status"),
                "full_ai_stage": item.get("full_ai_stage"),
                "full_ai_progress": item.get("full_ai_progress"),
                "full_ai_message": item.get("full_ai_message"),
                "full_ai_error": item.get("full_ai_error"),
                "sku_count": len((((evidence.get("supplier_page") or {}).get("skus")) or [])),
                "estimated_image_count": 3 + (len((((evidence.get("supplier_page") or {}).get("skus")) or [])) if len((((evidence.get("supplier_page") or {}).get("skus")) or [])) > 1 and (get_public_settings(root).get("strategy") or {}).get("generate_per_sku") is not False else 0),
                "weight_g": weight.get("value") or latest_package.get("weight_g"),
                "dimensions_cm": [
                    dimensions.get("length") or latest_package.get("length_cm"),
                    dimensions.get("width") or latest_package.get("width_cm"),
                    dimensions.get("height") or latest_package.get("height_cm"),
                ],
                "target_net_proceeds_usd": pricing.get("target_net_proceeds_usd"),
                "pricing_status": pricing.get("status") or "REAL_PROFIT_UNVERIFIED",
                "review_ready": not display_errors,
                "approved": bool(review_record and review_record.get("approved") is True),
                "publish_ready": not publish_issues,
                "publish_issues": publish_issues,
                "available_quantity": payload.get("available_quantity"),
                "sites_to_sell": payload.get("sites_to_sell") or [],
                "publish_status": publish_status,
                "publish_error": publish_error,
                "published_at": publish_record.get("published_at"),
                "publish_checked_at": publish_record.get("checked_at"),
                "publish_variants": publish_record.get("variants") or [],
            }
        )
    return rows


def draft_ai_log(name: str, drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> list[dict[str, Any]]:
    path = _draft_path(name, drafts_dir)
    events: list[dict[str, Any]] = []
    full_job_path = root / "jobs" / f"{path.stem}-full-ai-job.json"
    if full_job_path.is_file():
        job = read_json(full_job_path)
        events.append({"time": job.get("updated_at"), "event": "Full AI pipeline", "detail": {key: job.get(key) for key in ("status", "stage", "progress", "message", "error") if job.get(key) is not None}})
    image_job_path = root / "jobs" / f"{path.stem}-image-job.json"
    if image_job_path.is_file():
        job = read_json(image_job_path)
        events.append({"time": job.get("created_at"), "event": "Image generation submitted", "detail": {"status": job.get("status"), "prompt_count": len(job.get("prompts") or []), "language": job.get("language")}})
        for item in job.get("items") or []:
            events.append({"time": job.get("updated_at"), "event": f"Image {item.get('label') or item.get('kind')}", "detail": {key: item.get(key) for key in ("status", "kind", "error") if item.get(key) is not None}})
        if job.get("error"):
            events.append({"time": job.get("updated_at"), "event": "Image generation failed", "detail": {"error": job.get("error")}})
    proposal_path = root / "ai_outputs" / f"{path.stem}-proposal.json"
    if proposal_path.is_file():
        proposal = read_json(proposal_path)
        events.append({"time": proposal.get("created_at"), "event": "AI product content generated", "detail": {"status": proposal.get("status"), "model": proposal.get("model"), "response_id": proposal.get("response_id")}})
    events.append({"time": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "event": "Local draft updated", "detail": {"draft": path.name}})
    return sorted(events, key=lambda row: str(row.get("time") or ""), reverse=True)


def _save_full_job(path: Path, job: dict[str, Any], **changes: Any) -> None:
    job.update(changes)
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _wait_child_job(full_path: Path, job: dict[str, Any], child_path: Path, start: int, end: int, label: str) -> None:
    while True:
        latest_full = read_json(full_path)
        if latest_full.get("status") == "paused":
            raise EditorError("完整 AI 流程已暂停")
        child = read_json(child_path)
        items = child.get("items") or []
        done = sum(1 for row in items if row.get("status") == "done")
        fraction = done / max(1, len(items))
        _save_full_job(full_path, job, status="running", stage=label, progress=round(start + (end - start) * fraction), message=f"{label}：{done}/{len(items)}")
        if child.get("status") == "done":
            return
        if child.get("status") in {"failed", "paused", "cancelled"}:
            raise EditorError(str(child.get("error") or f"{label}未完成"))
        time.sleep(1)


def _prepare_multi_sku_ai_workflow(draft: dict[str, Any]) -> dict[str, Any]:
    """Freeze per-SKU facts before AI work; never blend one SKU into another."""
    evidence = draft.setdefault("evidence", {})
    details = [row for row in evidence.get("sku_details") or [] if isinstance(row, dict) and row.get("sku_id")]
    if len(details) < 2:
        evidence["multi_sku_ai"] = {"mode": "SINGLE_SKU", "status": "NOT_APPLICABLE", "items": []}
        return evidence["multi_sku_ai"]
    selected_id = str(evidence.get("current_pricing_sku_id") or details[0]["sku_id"])
    selected = next((row for row in details if str(row.get("sku_id")) == selected_id), details[0])
    evidence["current_pricing_sku_id"] = str(selected["sku_id"])
    payload = draft.setdefault("payload", {})
    payload["seller_sku"] = str(selected.get("seller_sku") or payload.get("seller_sku") or "")
    payload["available_quantity"] = int(selected.get("available_quantity") or 1000)
    pack = selected.get("package") if isinstance(selected.get("package"), dict) else {}
    if pack.get("weight_g") is not None:
        evidence["packed_weight"] = {"value": pack["weight_g"], "unit": "g", "status": "SUPPLIER_PACKAGING_INFO"}
    if all(pack.get(key) is not None for key in ("length_cm", "width_cm", "height_cm")):
        evidence["packed_dimensions"] = {"length": pack["length_cm"], "width": pack["width_cm"], "height": pack["height_cm"], "unit": "cm", "status": "SUPPLIER_PACKAGING_INFO"}
    pricing = draft.setdefault("pricing_plan", {})
    if selected.get("purchase_cost_cny") is not None:
        pricing["purchase_cost_cny"] = selected["purchase_cost_cny"]
    if selected.get("target_net_proceeds_usd") is not None:
        pricing["target_net_proceeds_usd"] = selected["target_net_proceeds_usd"]
    if selected.get("site_pricing"):
        payload["sites_to_sell"] = selected["site_pricing"]
    evidence["multi_sku_ai"] = {
        "mode": "MULTI_SKU", "status": "FACTS_FROZEN_FOR_AI", "current_pricing_sku_id": str(selected["sku_id"]),
        "public_content": "PENDING", "category_and_attributes": "PENDING", "pricing": "PENDING_CURRENT_SKU_ONLY",
        "images": "PENDING_PER_SKU", "publish_mode": "PENDING_CATEGORY_CHECK",
        "items": [{"sku_id": str(row["sku_id"]), "variant": str(row.get("variant") or ""), "facts": "READY", "pricing": "CURRENT" if row is selected else "NOT_SELECTED", "images": "PENDING"} for row in details],
    }
    return evidence["multi_sku_ai"]


def _apply_ai_content_and_attributes(path: Path, root: Path, full_path: Path, job: dict[str, Any]) -> None:
    draft = read_json(path)
    multi = _prepare_multi_sku_ai_workflow(draft)
    apply_inventory_rule(draft)
    vision = get_runtime_role(root, "vision")
    _save_full_job(full_path, job, stage="生成公共商品文案", progress=10, message="正在生成公共标题和描述；不会把各 SKU 的库存、价格或规格混入文案")
    model_config = {"base_url": vision["base_url"], "api_key_env": "YOYOU_VISION_API_KEY", "model": vision["model"], "_api_key": vision["api_key"]}
    proposal, response, quality_retries = generate_quality_checked_proposal(model_config, draft, vision["model"])
    proposal_path = root / "ai_outputs" / f"{path.stem}-proposal.json"
    proposal_record = {"status": "AI_PROPOSAL_REQUIRES_HUMAN_REVIEW", "source_draft": path.name, "model": vision["model"], "proposal": proposal, "response_id": response.get("id"), "usage": response.get("usage"), "quality_retries": quality_retries, "created_at": datetime.now(timezone.utc).isoformat(), "human_approved": False}
    write_json(proposal_path, proposal_record)
    versions = root / "draft_versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / f"{path.stem}-before-full-ai-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    locks = (draft.get("edit_metadata") or {}).get("field_locks") or {}
    payload = draft.setdefault("payload", {})
    previous_title = str(payload.get("title") or "")
    if not locks.get("title"):
        payload["title"] = _clean_text(proposal.get("title"), 60) or payload.get("title")
        payload["family_name"] = _clean_text(proposal.get("family_name"), 60) or payload.get("family_name")
    payload["description"] = _clean_text(proposal.get("description"), 5000) or payload.get("description")
    localized_titles = {
        str(row.get("site_id") or "").upper(): _clean_text(row.get("title"), 60)
        for row in proposal.get("site_titles") or []
        if isinstance(row, dict)
    }
    for index, raw in enumerate(payload.get("sites_to_sell") or []):
        if not isinstance(raw, dict):
            raw = {"site_id": raw}
            payload["sites_to_sell"][index] = raw
        site_id = str(raw.get("site_id") or "").upper()
        current_site_title = str(raw.get("title") or "").strip()
        if localized_titles.get(site_id) and (not current_site_title or current_site_title == previous_title):
            raw["title"] = localized_titles[site_id]

    _save_full_job(full_path, job, stage="匹配类目与必填属性", progress=25, message="正在查询美客多官方类目")
    current_category = str(payload.get("category_id") or "").upper()
    site = next((prefix for prefix in ("CBT", "MLM", "MLB", "MLC", "MLA", "MCO") if current_category.startswith(prefix)), "CBT")
    category_client = MercadoLibreCategoryClient()
    if not payload.get("category_id"):
        candidates = category_client.domain_discovery(site, payload.get("title") or payload.get("family_name") or "")
        if not candidates:
            raise EditorError("官方类目预测没有返回结果")
        payload["category_id"] = candidates[0].get("category_id")
        draft.setdefault("evidence", {})["category_suggestion"] = {"site_id": site, "selected": candidates[0], "alternatives": candidates[1:5], "status": "AI_ASSISTED_REQUIRES_HUMAN_REVIEW"}
    preflight = query_official_category(str(payload["category_id"]), root, category_client)
    if multi.get("mode") == "MULTI_SKU":
        allowed = int((preflight.get("settings") or {}).get("max_variations_allowed") or 0)
        count = len(multi.get("items") or [])
        multi["public_content"] = "READY_FOR_HUMAN_REVIEW"
        multi["category_and_attributes"] = "READY_FOR_HUMAN_REVIEW"
        multi["publish_mode"] = "VARIATION_REQUEST_NOT_IMPLEMENTED" if allowed >= count else "SINGLE_CURRENT_SKU_ONLY"
    official_attributes = preflight.get("all_attributes") or preflight.get("required_attributes") or []
    if official_attributes:
        attribute_role = get_runtime_role(root, "attributes")
        schema = {"type": "object", "additionalProperties": False, "properties": {"attributes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"id": {"type": "string"}, "value_id": {"type": ["string", "null"]}, "value_name": {"type": "string"}}, "required": ["id", "value_id", "value_name"]}}, "unresolved": {"type": "array", "items": {"type": "string"}}}, "required": ["attributes", "unresolved"]}
        context = {"proposal": proposal, "supplier": (draft.get("evidence") or {}).get("supplier_page") or {}, "official_attributes": official_attributes, "existing_attributes": payload.get("attributes") or []}
        request_body = {"model": attribute_role["model"], "store": False, "instructions": "Fill evidence-supported Mercado Libre official category attributes, including required and optional fields. Never invent brand, model, compatibility, GTIN, material, dimensions or performance. Return an empty value and add the field to unresolved when evidence is absent.", "input": json.dumps(context, ensure_ascii=False), "text": {"format": {"type": "json_schema", "name": "category_attributes", "strict": True, "schema": schema}}}
        attribute_response = call_responses_api({"base_url": attribute_role["base_url"], "api_key_env": "YOYOU_ATTRIBUTES_API_KEY", "model": attribute_role["model"], "_api_key": attribute_role["api_key"]}, request_body)
        result = json.loads(_output_text(attribute_response))
        allowed = {str(row.get("id")) for row in official_attributes}
        existing = {str(row.get("id")): row for row in payload.get("attributes") or [] if isinstance(row, dict) and row.get("id")}
        for row in result.get("attributes") or []:
            if str(row.get("id")) in allowed and (row.get("value_name") or row.get("value_id")):
                existing[str(row["id"])] = {"id": str(row["id"]), "value_id": row.get("value_id"), "value_name": _clean_text(row.get("value_name"), 500)}
        payload["attributes"] = list(existing.values())
        draft.setdefault("evidence", {})["attribute_completion"] = {"status": "AI_ASSISTED_REQUIRES_HUMAN_REVIEW", "unresolved": result.get("unresolved") or [], "category_id": payload["category_id"]}
    draft["human_approved"] = False
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def refresh_competitor_pricing(path: Path, root: Path = ROOT) -> dict[str, Any]:
    draft = read_json(path)
    analysis = collect_competitor_pricing(draft)
    draft.setdefault("evidence", {})["market_pricing_analysis"] = analysis
    draft.setdefault("pricing_plan", {})["profit_guidance"] = analysis.get("profit_guidance") or {}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return analysis


def _run_full_ai_flow(path: Path, root: Path, full_path: Path) -> None:
    job = read_json(full_path)
    try:
        _save_full_job(full_path, job, status="running", stage="建立 SKU 工作包", progress=3, message="正在冻结每个 SKU 的货源价、库存、包装、条码、规格和专属图片")
        draft = read_json(path)
        multi = _prepare_multi_sku_ai_workflow(draft)
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_full_job(full_path, job, status="running", stage="校验 SKU 工作包", progress=8, message=(f"已建立 {len(multi.get('items') or [])} 个 SKU 工作包；当前核价只使用已选择 SKU" if multi.get("mode") == "MULTI_SKU" else "单 SKU 商品，按常规流程处理"))
        errors, _warnings = validate(draft)
        if errors:
            raise EditorError("采集资料未齐：" + "；".join(errors))
        _apply_ai_content_and_attributes(path, root, full_path, job)
        _save_full_job(full_path, job, stage="当前核价 SKU：竞品与利润", progress=35, message="仅用当前核价 SKU 的采购价、包装重尺读取竞品并计算 10%–30% 利润区间")
        refresh_competitor_pricing(path, root)
        settings = get_public_settings(root)
        language = str((settings.get("strategy") or {}).get("image_language") or "auto")
        _save_full_job(full_path, job, stage="生成公共商品图", progress=40, message="正在提交3张公共商品图")
        start_image_generation(path.name, language, None, path.parent, root)
        _wait_child_job(full_path, job, root / "jobs" / f"{path.stem}-image-job.json", 40, 70, "生成公共商品图")
        skus = (((read_json(path).get("evidence") or {}).get("supplier_page") or {}).get("skus") or [])
        if len(skus) > 1 and (settings.get("strategy") or {}).get("generate_per_sku") is not False:
            _save_full_job(full_path, job, stage="生成各 SKU 专属图", progress=70, message=f"正在按各 SKU 自己的专属原图提交 {len(skus)} 张专属图")
            start_sku_image_generation(path.name, path.parent, root)
            _wait_child_job(full_path, job, root / "jobs" / f"{path.stem}-sku-image-job.json", 70, 98, "生成各 SKU 专属图")
        final_draft = read_json(path)
        final_multi = ((final_draft.get("evidence") or {}).get("multi_sku_ai") or {})
        if final_multi.get("mode") == "MULTI_SKU":
            final_multi["pricing"] = "READY_FOR_HUMAN_REVIEW"
            final_multi["images"] = "READY_FOR_HUMAN_REVIEW"
            for item in final_multi.get("items") or []:
                item["images"] = "READY_FOR_HUMAN_REVIEW"
            path.write_text(json.dumps(final_draft, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_full_job(full_path, job, status="done", stage="多 SKU 待审核", progress=100, message="公共文案、当前核价 SKU 定价和各 SKU 图片已分开处理，等待人工审核", error=None)
    except Exception as exc:
        latest = read_json(full_path)
        if latest.get("status") == "paused":
            return
        _save_full_job(full_path, job, status="failed", stage=job.get("stage") or "失败", message="完整 AI 流程失败，可查看日志后重试", error=str(exc)[:800])


def start_full_ai_flow(name: str, drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> dict[str, Any]:
    path = _draft_path(name, drafts_dir)
    settings = get_public_settings(root)
    missing = [row["label"] for row in settings["roles"].values() if not row.get("has_key")]
    if missing:
        raise EditorError("以下 API 尚未授权：" + "、".join(missing))
    skus = ((((read_json(path).get("evidence") or {}).get("supplier_page") or {}).get("skus") or []))
    image_count = 3 + (len(skus) if len(skus) > 1 and settings["strategy"].get("generate_per_sku") else 0)
    full_path = root / "jobs" / f"{path.stem}-full-ai-job.json"
    if full_path.is_file() and read_json(full_path).get("status") in {"queued", "running"}:
        raise EditorError("完整 AI 流程正在运行")
    now = datetime.now(timezone.utc).isoformat()
    job = {"status": "queued", "stage": "等待启动", "progress": 0, "message": "已加入完整 AI 流水线", "draft": path.name, "estimated_image_count": image_count, "created_at": now, "updated_at": now, "publishing_performed": False}
    full_path.parent.mkdir(parents=True, exist_ok=True)
    _save_full_job(full_path, job)
    Thread(target=_run_full_ai_flow, args=(path, root, full_path), daemon=True).start()
    return job


def batch_draft_action(action: str, names: list[Any], drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> dict[str, Any]:
    clean_names = [str(name) for name in names[:100]]
    if not clean_names:
        raise EditorError("请先选择商品。")
    results = []
    if action == "pause":
        for name in clean_names:
            path = _draft_path(name, drafts_dir)
            paused = []
            for suffix in ("full-ai-job", "image-job", "sku-image-job"):
                job_path = root / "jobs" / f"{path.stem}-{suffix}.json"
                if not job_path.is_file():
                    continue
                job = read_json(job_path)
                if job.get("status") in {"queued", "running", "generating"}:
                    job["status"] = "paused"
                    job["updated_at"] = datetime.now(timezone.utc).isoformat()
                    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                    paused.append(suffix)
            results.append({"name": name, "status": "paused" if paused else "no_active_job", "jobs": paused})
    elif action == "copy":
        for name in clean_names:
            source = _draft_path(name, drafts_dir)
            value = read_json(source)
            stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            destination = drafts_dir / f"{source.stem.removesuffix('-draft')}-copy-{stamp}-draft.json"
            value["human_approved"] = False
            value.setdefault("edit_metadata", {})["copied_from"] = source.name
            destination.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append({"name": name, "status": "copied", "copy": destination.name})
    elif action == "enter_review":
        versions_dir = root / "draft_versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        for name in clean_names:
            source = _draft_path(name, drafts_dir)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = versions_dir / f"{source.stem}-before-enter-review-{stamp}.json"
            backup.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            value = read_json(source)
            value["human_approved"] = False
            metadata = value.setdefault("edit_metadata", {})
            metadata["manual_review_requested_at"] = datetime.now(timezone.utc).isoformat()
            metadata["manual_review_source"] = "dashboard_batch"
            source.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append({"name": name, "status": "ready_for_human_review", "backup": str(backup)})
    elif action == "recycle":
        recycle_dir = root / "recycle_bin" / datetime.now().strftime("%Y%m%d")
        recycle_dir.mkdir(parents=True, exist_ok=True)
        for name in clean_names:
            source = _draft_path(name, drafts_dir)
            destination = recycle_dir / source.name
            if destination.exists():
                destination = recycle_dir / f"{source.stem}-{datetime.now().strftime('%H%M%S%f')}.json"
            source.replace(destination)
            results.append({"name": name, "status": "recycled", "restore_path": str(destination)})
    else:
        raise EditorError("不支持的批量操作。")
    return {"action": action, "results": results}


def get_draft(name: str, drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> dict[str, Any]:
    path = _draft_path(name, drafts_dir)
    draft = read_json(path)
    errors, warnings = validate(draft)
    proposal_path = root / "ai_outputs" / f"{path.stem}-proposal.json"
    proposal = read_json(proposal_path) if proposal_path.is_file() else None
    image_job_path = root / "jobs" / f"{path.stem}-image-job.json"
    image_job = read_json(image_job_path) if image_job_path.is_file() else None
    sku_image_job_path = root / "jobs" / f"{path.stem}-sku-image-job.json"
    sku_image_job = read_json(sku_image_job_path) if sku_image_job_path.is_file() else None
    review_path = root / "reviews" / f"{path.stem}-approval.json"
    review_record = read_json(review_path) if review_path.is_file() else None
    source_id = str((draft.get("evidence") or {}).get("source_product_id") or "").strip()
    latest_capture = None
    if source_id.isdigit():
        captures = sorted((root / "data" / "intake").glob(f"1688-{source_id}-*.json"))
        if captures:
            latest_capture = read_json(captures[-1])
            latest_capture = {
                "source_file": _relative_path(captures[-1], root),
                "captured_at": latest_capture.get("captured_at"),
                "title": latest_capture.get("title"),
                "description": latest_capture.get("description"),
                "pricing": latest_capture.get("pricing"),
                "pricing_tiers": latest_capture.get("pricing_tiers") or [],
                "minimum_order_quantity": latest_capture.get("minimum_order_quantity"),
                "package": latest_capture.get("package"),
                "domestic_shipping": latest_capture.get("domestic_shipping"),
                "collection_mode": latest_capture.get("collection_mode"),
                "skus": latest_capture.get("skus") or [],
                "image_counts": latest_capture.get("image_counts") or {},
                "product_images": latest_capture.get("product_images") or latest_capture.get("images") or [],
                "sku_images": latest_capture.get("sku_images") or [],
                "detail_images": latest_capture.get("detail_images") or [],
                "extraction_source": latest_capture.get("extraction_source"),
            }
    return {
        "name": path.name,
        "draft": draft,
        "validation": {"errors": errors, "warnings": warnings},
        "ai_proposal": proposal,
        "image_prompts": (image_job or {}).get("prompts") or build_image_prompts(draft),
        "image_job": image_job,
        "sku_image_job": sku_image_job,
        "collected_skus": ((draft.get("evidence") or {}).get("supplier_page") or {}).get("skus") or [],
        "sku_details": ((draft.get("evidence") or {}).get("sku_details") or []),
        "review_record": review_record,
        "latest_capture": latest_capture,
        "workflow": workflow_item(path, root),
        "mode": "LOCAL_DRAFT_ONLY",
        "publishing_performed": False,
    }


def approve_local_review(name: str, drafts_dir: Path = DRAFTS_DIR, root: Path = ROOT) -> dict[str, Any]:
    row = next((item for item in list_drafts(drafts_dir, root) if item["name"] == unquote(name)), None)
    if not row:
        raise EditorError("没有找到草稿")
    if row.get("stage") != "READY_FOR_HUMAN_REVIEW":
        raise EditorError("商品处理尚未完成，不能进入审核")
    if not row.get("review_ready"):
        raise EditorError("资料未齐，不能通过审核")
    draft = read_json(_draft_path(name, drafts_dir))
    issues = publish_preflight_issues(draft)
    if issues:
        raise EditorError(f"发布前检查未通过：{issues[0]}")
    review_dir = root / "reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    destination = review_dir / f"{Path(row['name']).stem}-approval.json"
    record = {
        "approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "draft": row["name"],
        "next_stage": "READY_TO_PUBLISH",
        "external_publish_performed": False,
    }
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def update_review_gallery(
    name: str,
    gallery: list[dict[str, Any]],
    drafts_dir: Path = DRAFTS_DIR,
    versions_dir: Path = VERSIONS_DIR,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Persist an explicitly reviewed listing gallery with a reversible backup."""
    if not isinstance(gallery, list):
        raise EditorError("上架图片格式不正确")
    source_path = _draft_path(name, drafts_dir)
    draft = read_json(source_path)
    current_review = ((draft.get("evidence") or {}).get("image_review") or {})
    allowed_remote: set[str] = set()
    for pool in (current_review.get("source_gallery") or [], current_review.get("detail_gallery") or [], current_review.get("gallery") or []):
        for item in pool:
            if isinstance(item, dict) and item.get("remote_url"):
                allowed_remote.add(str(item["remote_url"]))
    source_id = str((draft.get("evidence") or {}).get("source_product_id") or "")
    if source_id.isdigit():
        captures = sorted((root / "data" / "intake").glob(f"1688-{source_id}-*.json"))
        if captures:
            latest = read_json(captures[-1])
            allowed_remote.update(str(url) for url in (latest.get("product_images") or []) if url)
            allowed_remote.update(str(row.get("url")) for row in (latest.get("sku_images") or []) if isinstance(row, dict) and row.get("url"))

    job_path = root / "jobs" / f"{source_path.stem}-image-job.json"
    allowed_generated: set[str] = set()
    if job_path.is_file():
        for item in read_json(job_path).get("items") or []:
            if isinstance(item, dict) and item.get("status") == "done" and item.get("local_file"):
                allowed_generated.add(str(item["local_file"]).removeprefix("assets/"))

    safe_gallery: list[dict[str, Any]] = []
    seen: set[str] = set()
    assets_root = (root / "assets").resolve()
    for raw in gallery[:20]:
        if not isinstance(raw, dict):
            continue
        remote_url = str(raw.get("remote_url") or "")
        local_file = str(raw.get("local_file") or "").removeprefix("assets/")
        remote_ok = remote_url in allowed_remote or (raw.get("source") == "linked" and urlparse(remote_url).scheme == "https")
        local_pattern_ok = re.fullmatch(r"(?:uploads|generated|ai-generated)/[A-Za-z0-9._/-]+\.(?:png|jpg|jpeg|webp)", local_file, re.I) is not None
        local_path = (assets_root / local_file).resolve() if local_file else None
        local_ok = bool(local_pattern_ok and local_path and assets_root in local_path.parents and local_path.is_file())
        generated_ok = local_file in allowed_generated and local_ok
        if not (remote_ok or local_ok or generated_ok):
            continue
        key = local_file or remote_url
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {
            "role": "main" if not safe_gallery else f"listing_{len(safe_gallery) + 1}",
            "review_status": "HUMAN_SELECTED",
            "source": _clean_text(raw.get("source"), 40) or ("local_edit" if local_file else "source_gallery"),
        }
        if remote_url:
            item["remote_url"] = remote_url
        if local_file:
            item["local_file"] = local_file
        if raw.get("derived_from"):
            item["derived_from"] = _clean_text(raw.get("derived_from"), 1000)
        safe_gallery.append(item)
    if not safe_gallery:
        raise EditorError("上架图片至少保留 1 张")

    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = versions_dir / f"{source_path.stem}-before-review-images-{stamp}.json"
    backup.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    image_review = draft.setdefault("evidence", {}).setdefault("image_review", {})
    image_review["gallery"] = safe_gallery
    draft["evidence"]["images_human_reviewed"] = True
    draft.setdefault("edit_metadata", {}).setdefault("field_locks", {})["pictures"] = True
    draft["edit_metadata"]["images_updated_at"] = datetime.now(timezone.utc).isoformat()
    draft["human_approved"] = False
    temporary = source_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(source_path)
    return {"gallery": safe_gallery, "backup": str(backup), "publishing_performed": False}


def update_review_sku_galleries(
    name: str,
    sku_galleries: list[dict[str, Any]],
    drafts_dir: Path = DRAFTS_DIR,
    versions_dir: Path = VERSIONS_DIR,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Persist human ordering/removal for composed SKU galleries."""
    if not isinstance(sku_galleries, list):
        raise EditorError("SKU 图片调整格式不正确")
    source_path = _draft_path(name, drafts_dir)
    draft = read_json(source_path)
    evidence = draft.setdefault("evidence", {})
    review = evidence.setdefault("image_review", {})
    known_skus = {
        str(row.get("sku_id") or ""): str(row.get("variant") or "")
        for row in ((evidence.get("supplier_page") or {}).get("skus") or []) if isinstance(row, dict)
    }
    allowed_shared = {
        str(row.get("local_file") or row.get("remote_url") or "")
        for row in (review.get("gallery") or []) if isinstance(row, dict)
    }
    allowed_sku_source: dict[str, set[str]] = {}
    for group in review.get("sku_source_galleries") or []:
        if not isinstance(group, dict) or not group.get("sku_id"):
            continue
        allowed_sku_source[str(group["sku_id"])] = {
            str(image.get("local_file") or image.get("remote_url") or "")
            for image in group.get("gallery") or [] if isinstance(image, dict)
        }
    sku_job_path = root / "jobs" / f"{source_path.stem}-sku-image-job.json"
    allowed_heroes: set[str] = set()
    if sku_job_path.is_file():
        allowed_heroes = {
            str(row.get("local_file") or "").removeprefix("assets/")
            for row in (read_json(sku_job_path).get("items") or [])
            if isinstance(row, dict) and row.get("status") == "done" and row.get("local_file")
        }
    cleaned: list[dict[str, Any]] = []
    seen_skus: set[str] = set()
    for raw in sku_galleries[:100]:
        if not isinstance(raw, dict):
            continue
        sku_id = str(raw.get("sku_id") or "")
        if not sku_id or sku_id not in known_skus or sku_id in seen_skus:
            continue
        gallery: list[dict[str, Any]] = []
        seen_images: set[str] = set()
        for image in (raw.get("gallery") or [])[:8]:
            if not isinstance(image, dict):
                continue
            local = str(image.get("local_file") or "").removeprefix("assets/")
            remote = str(image.get("remote_url") or "")
            key = local or remote
            local_path = (root / "assets" / local).resolve() if local else None
            upload_ok = bool(
                local and image.get("source") in {"local_upload", "local_edit"}
                and re.fullmatch(r"uploads/[a-f0-9]{64}\.(?:png|jpg|jpeg|webp)", local, re.I)
                and local_path and (root / "assets").resolve() in local_path.parents and local_path.is_file()
            )
            source_ok = key in allowed_sku_source.get(sku_id, set())
            if not key or key in seen_images or (key not in allowed_shared and not source_ok and local not in allowed_heroes and not upload_ok):
                continue
            row = {"role": "main" if not gallery else f"listing_{len(gallery) + 1}", "source": _clean_text(image.get("source"), 40) or "sku_gallery", "review_status": "HUMAN_SELECTED"}
            if local:
                row["local_file"] = local
            if remote:
                row["remote_url"] = remote
            if local in allowed_heroes:
                row.update({"sku_id": sku_id, "variant": known_skus[sku_id], "source": "sku_ai_generated"})
            gallery.append(row)
            seen_images.add(key)
        if gallery:
            cleaned.append({"sku_id": sku_id, "variant": known_skus[sku_id], "gallery": gallery, "review_status": "HUMAN_SELECTED"})
            seen_skus.add(sku_id)
    if not cleaned:
        raise EditorError("每个 SKU 至少保留 1 张上架图片")
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = versions_dir / f"{source_path.stem}-before-review-sku-images-{stamp}.json"
    backup.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    review["sku_listing_galleries"] = cleaned
    review["sku_gallery_updated_at"] = datetime.now(timezone.utc).isoformat()
    evidence["sku_images_human_reviewed"] = True
    draft["human_approved"] = False
    temporary = source_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(source_path)
    return {"sku_listing_galleries": cleaned, "backup": str(backup), "publishing_performed": False}


def start_image_generation(
    name: str,
    language: str,
    prompts: list[dict[str, str]] | None = None,
    drafts_dir: Path = DRAFTS_DIR,
    root: Path = ROOT,
) -> dict[str, Any]:
    """建立异步生图任务；只保存提示词、状态和图片路径。"""
    path = _draft_path(name, drafts_dir)
    draft = read_json(path)
    safe_language = resolve_market_language(draft, language)
    generated = build_image_prompts(draft, safe_language)
    if prompts:
        prompt_map = {str(row.get("kind")): str(row.get("prompt") or "").strip()[:8000] for row in prompts if isinstance(row, dict)}
        for row in generated:
            if prompt_map.get(row["kind"]):
                row["prompt"] = prompt_map[row["kind"]]
    evidence = draft.get("evidence") or {}
    source_gallery = ((evidence.get("image_review") or {}).get("source_gallery") or (evidence.get("image_review") or {}).get("gallery") or [])[:3]
    source_paths: list[Path] = []
    for row in source_gallery:
        if not isinstance(row, dict):
            continue
        local = str(row.get("local_file") or "").removeprefix("assets/")
        if local:
            candidate = (root / "assets" / local).resolve()
            if candidate.is_file() and (root / "assets").resolve() in candidate.parents:
                source_paths.append(candidate)
        elif row.get("remote_url"):
            cached_path, _content_type = cache_remote_image(str(row["remote_url"]), root / "assets" / "remote-cache")
            source_paths.append(cached_path)
    if not source_paths:
        raise EditorError("没有可用的商品原图，不能启动 AI 生图。")
    job_path = root / "jobs" / f"{path.stem}-image-job.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "status": "queued",
        "draft": path.name,
        "language": safe_language,
        "prompts": generated,
        "items": [{"index": index, "kind": row["kind"], "label": row["label"], "status": "queued"} for index, row in enumerate(generated)],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "publishing_performed": False,
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    Thread(target=run_job, args=(root, path.name, generated, source_paths, job_path), daemon=True).start()
    return job


def start_sku_image_generation(
    name: str,
    drafts_dir: Path = DRAFTS_DIR,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Queue one paid, text-free hero image for every collected SKU."""
    path = _draft_path(name, drafts_dir)
    draft = read_json(path)
    prompts = build_sku_image_prompts(draft)
    if not prompts:
        raise EditorError("没有取得可用的 SKU 规格，不能生成 SKU 专属图。")
    settings = get_public_settings(root)
    reference_count = int((settings.get("strategy") or {}).get("reference_image_count") or 3)
    review = ((draft.get("evidence") or {}).get("image_review") or {})
    source_gallery = (review.get("source_gallery") or review.get("gallery") or [])[:reference_count]
    def image_paths(rows: list[dict[str, Any]]) -> list[Path]:
        paths: list[Path] = []
        for row in rows[:reference_count]:
            if not isinstance(row, dict):
                continue
            local = str(row.get("local_file") or "").removeprefix("assets/")
            if local:
                candidate = (root / "assets" / local).resolve()
                if candidate.is_file() and (root / "assets").resolve() in candidate.parents:
                    paths.append(candidate)
            elif row.get("remote_url"):
                cached, _ = cache_remote_image(str(row["remote_url"]), root / "assets" / "remote-cache")
                paths.append(cached)
        return paths
    source_paths = image_paths(source_gallery)
    source_paths_by_sku: dict[str, list[Path]] = {}
    for group in review.get("sku_source_galleries") or []:
        if isinstance(group, dict) and group.get("sku_id"):
            paths = image_paths(group.get("gallery") or [])
            if paths:
                source_paths_by_sku[str(group["sku_id"])] = paths
    if not source_paths:
        raise EditorError("没有可用的商品原图，不能启动 SKU 专属生图。")
    job_path = root / "jobs" / f"{path.stem}-sku-image-job.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    job = {
        "status": "queued", "draft": path.name, "job_type": "sku_images",
        "reference_mode": "per_sku_source_images_with_shared_fallback", "reference_count": len(source_paths),
        "per_sku_reference_counts": {sku_id: len(paths) for sku_id, paths in source_paths_by_sku.items()},
        "prompts": prompts,
        "items": [{"index": i, "kind": row["kind"], "label": row["label"], "sku_id": row["sku_id"], "variant": row["variant"], "status": "queued"} for i, row in enumerate(prompts)],
        "created_at": now, "updated_at": now, "publishing_performed": False,
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    Thread(target=run_job, args=(root, path.name, prompts, source_paths, job_path, source_paths_by_sku), daemon=True).start()
    return job


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def query_official_category(
    category_id: str,
    root: Path = ROOT,
    client: Any | None = None,
) -> dict[str, Any]:
    clean = str(category_id or "").strip().upper()
    if not SAFE_CATEGORY_ID.fullmatch(clean):
        raise EditorError("类目 ID 格式不正确")
    cache_path = root / "data" / "categories" / f"{clean}.json"
    try:
        result = category_preflight(client or MercadoLibreCategoryClient(), clean)
        result["query_source"] = "MERCADOLIBRE_OFFICIAL_API_LIVE"
        result["queried_at"] = datetime.now(timezone.utc).isoformat()
        write_json(cache_path, result)
        return result
    except (MercadoLibreAPIError, OSError) as exc:
        if cache_path.is_file():
            cached = read_json(cache_path)
            cached["query_source"] = "MERCADOLIBRE_OFFICIAL_API_CACHE"
            cached["live_query_error"] = str(exc)
            return cached
        raise EditorError(str(exc)) from exc


def discover_official_category(
    title: str,
    site_id: str,
    category_id: str = "",
    root: Path = ROOT,
    client: MercadoLibreCategoryClient | None = None,
) -> dict[str, Any]:
    """Find a Mercado Libre category, then fetch its current official attributes."""
    site = str(site_id or "CBT").strip().upper()
    if site not in {"CBT", "MLM", "MLB", "MLC", "MLA", "MCO"}:
        raise EditorError("暂不支持该美客多站点")
    api = client or MercadoLibreCategoryClient()
    clean_category = str(category_id or "").strip().upper()
    candidates: list[dict[str, Any]] = []
    if clean_category:
        selected_id = clean_category
    else:
        query = _clean_text(title, 180)
        if not query:
            raise EditorError("缺少商品标题，无法匹配类目")
        if re.search(r"[\u3400-\u9fff]", query):
            raise EditorError("中文采集标题不能直接用于官方类目预测；请先填写类目 ID，或使用“查询类目并 AI 回填属性”。")
        candidates = api.domain_discovery(site, query)
        if not candidates or not candidates[0].get("category_id"):
            raise EditorError("美客多官方类目预测没有返回结果")
        normalized = re.sub(r"[^a-z0-9]+", " ", query.lower())
        if site == "CBT" and re.search(r"\b(recoil starter|pull starter|pull start)\b", normalized):
            if not any(str(row.get("domain_id") or "").upper().endswith("-GARDEN_MACHINE_RECOIL_STARTERS") for row in candidates):
                focused = api.domain_discovery(site, "garden machine recoil starter")
                known = {str(row.get("category_id") or "") for row in candidates}
                candidates.extend(row for row in focused if str(row.get("category_id") or "") not in known)
        selected = select_category_candidate(query, candidates) or candidates[0]
        selected_id = str(selected["category_id"])
    category = query_official_category(selected_id, root, api)
    return {"site_id": site, "selected": category, "candidates": candidates[:8]}


def browse_official_categories(
    site_id: str,
    parent_id: str = "",
    query: str = "",
    client: MercadoLibreCategoryClient | None = None,
) -> dict[str, Any]:
    """Browse the public Mercado Libre category tree without seller OAuth."""
    site = str(site_id or "CBT").strip().upper()
    if site not in {"CBT", "MLM", "MLB", "MLC", "MLA", "MCO"}:
        raise EditorError("暂不支持该美客多站点")
    api = client or MercadoLibreCategoryClient()
    parent = str(parent_id or "").strip().upper()
    if not parent:
        try:
            children = api.site_categories(site)
            suggested = False
        except MercadoLibreAPIError:
            # CBT's root tree may require the user's official OAuth token even
            # though individual category reads remain public.
            try:
                children = MercadoLibreClient().get(f"/sites/{site}/categories")
                suggested = False
            except MercadoLibreAPIError:
                phrase = _clean_text(query, 180)
                if not phrase or re.search(r"[\u3400-\u9fff]", phrase):
                    raise EditorError("CBT 全量分类树需要重新授权；当前可先用类目 ID 或 AI 分类词查询。")
                candidates = api.domain_discovery(site, phrase)
                children = []
                for row in candidates[:12]:
                    category_id = str(row.get("category_id") or "")
                    if not category_id:
                        continue
                    detail = api.category(category_id)
                    children.append({"id": category_id, "name": detail.get("name") or row.get("category_name"), "path_from_root": detail.get("path_from_root") or [], "selectable": True})
                suggested = True
        path: list[dict[str, Any]] = []
    else:
        category = api.category(parent)
        children = category.get("children_categories") or []
        path = category.get("path_from_root") or []
    return {
        "site_id": site,
        "parent_id": parent or None,
        "path_from_root": [{"id": row.get("id"), "name": row.get("name")} for row in path],
        "children": [{"id": row.get("id"), "name": row.get("name"), "total_items_in_this_category": row.get("total_items_in_this_category"), "path_from_root": row.get("path_from_root") or [], "selectable": row.get("selectable") is True} for row in children],
        "is_leaf": bool(parent and not children),
        "suggested": suggested if not parent else False,
    }


def _category_search_query(draft: dict[str, Any], title: str, site_id: str, root: Path) -> str:
    """Create an internal discovery phrase; it is never used as listing copy."""
    role = get_runtime_role(root, "vision")
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"query": {"type": "string", "maxLength": 100}},
        "required": ["query"],
    }
    evidence = draft.get("evidence") or {}
    raw_attributes = ((evidence.get("supplier_page") or {}).get("attributes") or {})
    safe_attributes = {str(key)[:80]: _clean_text(value, 240) for key, value in list(raw_attributes.items())[:40]}
    context = {
        "source_title": _clean_text(title, 300),
        "supplier_attributes": safe_attributes,
        "selected_variant": ((evidence.get("supplier_page") or {}).get("selected_variant") or []),
        "site_id": site_id,
    }
    request_body = {
        "model": role["model"], "store": False,
        "instructions": "Create one short English product taxonomy search phrase for Mercado Libre category discovery. Use only the supplied evidence. Name the product type first, then only proven machine type or model identifiers. Do not add marketing claims. This phrase is internal metadata and will never be published.",
        "input": json.dumps(context, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "category_search_query", "strict": True, "schema": schema}},
    }
    response = call_responses_api(
        {"base_url": role["base_url"], "api_key_env": "YOYOU_VISION_API_KEY", "model": role["model"], "_api_key": role["api_key"]}, request_body,
    )
    query = _clean_text(json.loads(_output_text(response)).get("query"), 100)
    if not query or re.search(r"[\u3400-\u9fff]", query):
        raise EditorError("AI 未能生成可靠的类目查询词")
    return query


def suggest_required_attributes(
    name: str,
    site_id: str,
    title: str,
    category_id: str,
    current_attributes: list[Any] | None,
    drafts_dir: Path = DRAFTS_DIR,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Return evidence-bound attribute suggestions without silently saving them."""
    path = _draft_path(name, drafts_dir)
    draft = read_json(path)
    discovery_title = title
    if not str(category_id or "").strip() and re.search(r"[\u3400-\u9fff]", str(title or "")):
        discovery_title = _category_search_query(draft, title, site_id, root)
    found = discover_official_category(discovery_title, site_id, category_id, root)
    found["internal_category_query"] = discovery_title if discovery_title != title else None
    category = found["selected"]
    official_attributes = category.get("all_attributes") or category.get("required_attributes") or []
    role = get_runtime_role(root, "attributes")
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "attributes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "value_id": {"type": ["string", "null"]}, "value_name": {"type": "string"}},
                "required": ["id", "value_id", "value_name"],
            }},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["attributes", "unresolved"],
    }
    context = {
        "site_id": found["site_id"], "category": category, "title": _clean_text(title, 300),
        "supplier_evidence": (draft.get("evidence") or {}).get("supplier_page") or {},
        "physical_evidence": {
            "packed_weight": (draft.get("evidence") or {}).get("packed_weight") or {},
            "packed_dimensions": (draft.get("evidence") or {}).get("packed_dimensions") or {},
        },
        "current_attributes": current_attributes or (draft.get("payload") or {}).get("attributes") or [],
        "official_attributes": official_attributes,
    }
    request_body = {
        "model": role["model"], "store": False,
        "instructions": "根据给定采集证据填写美客多官方类目属性，包括有证据支持的必填与可选属性。只能使用证据中明确存在的事实和官方允许值；禁止编造品牌、型号、兼容性、GTIN、材质、尺寸、数量或性能。无法确认时 value_name 留空并把属性 ID 放入 unresolved。输出字段值使用目标站点语言，不要输出英文营销文案。",
        "input": json.dumps(context, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "required_attributes", "strict": True, "schema": schema}},
    }
    response = call_responses_api(
        {"base_url": role["base_url"], "api_key_env": "YOYOU_ATTRIBUTES_API_KEY", "model": role["model"], "_api_key": role["api_key"]}, request_body,
    )
    result = json.loads(_output_text(response))
    allowed = {str(row.get("id")) for row in official_attributes if row.get("id")}
    suggestions = [row for row in (result.get("attributes") or []) if str(row.get("id")) in allowed]
    return {**found, "attributes": suggestions, "unresolved": result.get("unresolved") or [], "usage": response.get("usage")}


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EditorError("重量、尺寸、库存必须是数字") from exc
    if number < 0:
        raise EditorError("重量、尺寸、库存不能是负数")
    return number


def cache_remote_image(url: str, cache_dir: Path) -> tuple[Path, str]:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or not IMAGE_HOST.search(parsed.hostname):
        raise EditorError("图片地址不在允许范围")
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    existing = next(cache_dir.glob(f"{key}.*"), None)
    if existing:
        return existing, mimetypes.guess_type(existing.name)[0] or "image/jpeg"
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://detail.1688.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    with urlopen(request, timeout=12) as response:
        content_type = str(response.headers.get_content_type() or "")
        if not content_type.startswith("image/"):
            raise EditorError("远程地址不是图片")
        body = response.read(8_000_001)
    if not body or len(body) > 8_000_000:
        raise EditorError("图片为空或超过 8MB")
    suffix = mimetypes.guess_extension(content_type) or ".jpg"
    if suffix == ".jpe":
        suffix = ".jpg"
    destination = cache_dir / f"{key}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(destination)
    return destination, content_type


def save_uploaded_image(name: str, data_url: str, assets_dir: Path) -> str:
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)", str(data_url or ""))
    if not match:
        raise EditorError("只支持 PNG、JPG、WEBP 图片")
    try:
        body = base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise EditorError("图片内容格式不正确") from exc
    if not body or len(body) > 8_000_000:
        raise EditorError("图片为空或超过 8MB")
    signatures = {
        "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
        "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
        "image/webp": (b"RIFF", ".webp"),
    }
    signature, suffix = signatures[match.group(1)]
    if not body.startswith(signature) or (match.group(1) == "image/webp" and body[8:12] != b"WEBP"):
        raise EditorError("图片真实格式与文件类型不一致")
    target_dir = assets_dir / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"{hashlib.sha256(body).hexdigest()}{suffix}"
    if not destination.exists():
        destination.write_bytes(body)
    return f"uploads/{destination.name}"


def create_edited_version(
    name: str,
    changes: dict[str, Any],
    drafts_dir: Path = DRAFTS_DIR,
    versions_dir: Path = VERSIONS_DIR,
) -> Path:
    if not isinstance(changes, dict) or _contains_forbidden_key(changes):
        raise EditorError("编辑内容包含不允许保存的字段")
    source_path = _draft_path(name, drafts_dir)
    source = read_json(source_path)
    edited = copy.deepcopy(source)
    payload = edited.setdefault("payload", {})
    evidence = edited.setdefault("evidence", {})

    fields = changes.get("payload") if isinstance(changes.get("payload"), dict) else {}
    payload["title"] = _clean_text(fields.get("title", payload.get("title")), 300)
    payload["family_name"] = _clean_text(fields.get("family_name", payload.get("family_name")), 300)
    payload["description"] = _clean_text(fields.get("description", payload.get("description")), 10000)
    payload["category_id"] = _clean_text(fields.get("category_id", payload.get("category_id")), 50) or None
    payload["currency_id"] = _clean_text(fields.get("currency_id", payload.get("currency_id") or "USD"), 8) or "USD"
    quantity = _number(fields.get("available_quantity", payload.get("available_quantity")))
    payload["available_quantity"] = int(quantity) if quantity is not None else None
    payload["seller_sku"] = _clean_text(fields.get("seller_sku", payload.get("seller_sku")), 120)
    payload["barcode_type"] = _clean_text(fields.get("barcode_type", payload.get("barcode_type") or "GTIN"), 20) or "GTIN"
    payload["buying_mode"] = _clean_text(fields.get("buying_mode", payload.get("buying_mode") or "buy_it_now"), 30) or "buy_it_now"
    payload["condition"] = _clean_text(fields.get("condition", payload.get("condition") or "new"), 30) or "new"
    payload["item_condition_value_id"] = _clean_text(fields.get("item_condition_value_id", payload.get("item_condition_value_id") or "2230284"), 50)
    payload["catalog_listing"] = fields.get("catalog_listing") is True
    payload["warranty_type"] = _clean_text(fields.get("warranty_type", payload.get("warranty_type") or "No warranty"), 120)
    payload["warranty_type_value_id"] = _clean_text(fields.get("warranty_type_value_id", payload.get("warranty_type_value_id") or "6150835"), 50)
    payload["warranty_time"] = _clean_text(fields.get("warranty_time", payload.get("warranty_time")), 120)

    allowed_sites = {"MLM", "MLC", "MLB", "MLA", "MCO"}
    sites = fields.get("sites_to_sell") if isinstance(fields.get("sites_to_sell"), list) else payload.get("sites_to_sell") or []
    clean_sites: list[dict[str, Any]] = []
    for raw in sites:
        row = raw if isinstance(raw, dict) else {"site_id": raw}
        site_id = _clean_text(row.get("site_id"), 8).upper()
        if site_id not in allowed_sites:
            continue
        clean_sites.append({
            "site_id": site_id,
            "price": _number(row.get("price")),
            "net_proceeds": _number(row.get("net_proceeds")),
            "shipping_cost_usd": _number(row.get("shipping_cost_usd")),
            "logistic_type": _clean_text(row.get("logistic_type"), 40) or "remote",
            "listing_type_id": _clean_text(row.get("listing_type_id"), 60) or "gold_special",
            "title": _clean_text(row.get("title"), 300) or payload.get("title"),
        })
    payload["sites_to_sell"] = clean_sites

    attributes: list[dict[str, Any]] = []
    attribute_sources: dict[str, str] = {}
    for row in fields.get("attributes") or []:
        if not isinstance(row, dict):
            continue
        attribute_id = _clean_text(row.get("id"), 80)
        if not attribute_id:
            continue
        attributes.append(
            {
                "id": attribute_id,
                "value_id": _clean_text(row.get("value_id"), 120) or None,
                "value_name": _clean_text(row.get("value_name"), 500),
            }
        )
        attribute_sources[attribute_id] = _clean_text(row.get("source"), 120) or "unresolved"
        if len(attributes) >= 120:
            break
    payload["attributes"] = attributes

    physical = changes.get("physical") if isinstance(changes.get("physical"), dict) else {}
    weight = _number(physical.get("weight_g"))
    length = _number(physical.get("length_cm"))
    width = _number(physical.get("width_cm"))
    height = _number(physical.get("height_cm"))
    status = _clean_text(physical.get("status"), 80) or "HUMAN_EDITED_UNVERIFIED"
    evidence["packed_weight"] = ({"value": weight, "unit": "g", "status": status} if weight is not None else None)
    evidence["packed_dimensions"] = (
        {"length": length, "width": width, "height": height, "unit": "cm", "status": status}
        if None not in (length, width, height)
        else None
    )
    actual_kg = weight / 1000 if weight is not None else None
    volumetric_kg = (length * width * height / 6000) if None not in (length, width, height) else None
    evidence["shipping_weight_calculation"] = {
        "actual_weight_kg": actual_kg,
        "volumetric_weight_kg": volumetric_kg,
        "billable_weight_kg": max(value for value in (actual_kg, volumetric_kg) if value is not None) if any(value is not None for value in (actual_kg, volumetric_kg)) else None,
        "divisor": 6000,
        "confidence": _clean_text(physical.get("confidence"), 20) or "UNVERIFIED",
    }

    evidence["gtin_or_exemption"] = _clean_text(changes.get("gtin_or_exemption", evidence.get("gtin_or_exemption")), 1000)
    evidence["compatibility_evidence"] = _clean_text(changes.get("compatibility_evidence", evidence.get("compatibility_evidence")), 3000)
    evidence["current_pricing_sku_id"] = _clean_text(changes.get("current_pricing_sku_id", evidence.get("current_pricing_sku_id")), 120)
    incoming_skus = changes.get("sku_details") if isinstance(changes.get("sku_details"), list) else evidence.get("sku_details") or []
    clean_sku_details = []
    for row in incoming_skus[:500]:
        if not isinstance(row, dict):
            continue
        package_row = row.get("package") if isinstance(row.get("package"), dict) else {}
        sku_id = _clean_text(row.get("sku_id"), 120)
        if not sku_id:
            continue
        clean_sku_details.append({
            "sku_id": sku_id, "spec_id": _clean_text(row.get("spec_id"), 120), "variant": _clean_text(row.get("variant"), 300),
            "seller_sku": _clean_text(row.get("seller_sku"), 120), "purchase_cost_cny": _number(row.get("purchase_cost_cny")),
            "listing_price_usd": _number(row.get("listing_price_usd")), "target_net_proceeds_usd": _number(row.get("target_net_proceeds_usd")),
            "available_quantity": int(_number(row.get("available_quantity")) or 0) or 1000,
            "inventory_source": _clean_text(row.get("inventory_source"), 80),
            "package": {key: _number(package_row.get(key)) for key in ("weight_g", "length_cm", "width_cm", "height_cm")},
            "barcode_type": _clean_text(row.get("barcode_type"), 20) or "NO_GTIN", "gtin": _clean_text(row.get("gtin"), 120),
            "variation_attributes": [
                {"name": _clean_text(item.get("name"), 80), "value": _clean_text(item.get("value"), 300)}
                for item in row.get("variation_attributes") or [] if isinstance(item, dict) and _clean_text(item.get("name"), 80)
            ][:20],
            "source_image_count": int(_number(row.get("source_image_count")) or 0),
            "site_pricing": [
                {
                    "site_id": _clean_text(item.get("site_id"), 8).upper(), "price": _number(item.get("price")),
                    "net_proceeds": _number(item.get("net_proceeds")), "shipping_cost_usd": _number(item.get("shipping_cost_usd")),
                    "logistic_type": _clean_text(item.get("logistic_type"), 40) or "remote",
                    "listing_type_id": _clean_text(item.get("listing_type_id"), 60) or "gold_special",
                    "title": _clean_text(item.get("title"), 300),
                }
                for item in row.get("site_pricing") or []
                if isinstance(item, dict) and _clean_text(item.get("site_id"), 8).upper() in {"MLM", "MLC", "MLB", "MLA", "MCO"}
            ][:10],
        })
    evidence["sku_details"] = clean_sku_details
    evidence["images_human_reviewed"] = changes.get("images_human_reviewed") is True

    pricing = changes.get("pricing") if isinstance(changes.get("pricing"), dict) else {}
    edited["pricing_plan"] = {
        "mode": "net_proceeds",
        "purchase_cost_cny": _number(pricing.get("purchase_cost_cny")),
        "domestic_shipping_cny": _number(pricing.get("domestic_shipping_cny")),
        "exchange_rate_cny_per_usd": _number(pricing.get("exchange_rate_cny_per_usd")),
        "packaging_cost_usd": _number(pricing.get("packaging_cost_usd")),
        "cross_border_freight_usd": _number(pricing.get("cross_border_freight_usd")),
        "other_cost_usd": _number(pricing.get("other_cost_usd")),
        "target_net_proceeds_usd": _number(pricing.get("target_net_proceeds_usd")),
    }
    current_sku_id = str(evidence.get("current_pricing_sku_id") or "")
    selected_sku = next((row for row in clean_sku_details if row["sku_id"] == current_sku_id), None)
    if selected_sku:
        if selected_sku.get("target_net_proceeds_usd") is not None:
            edited["pricing_plan"]["target_net_proceeds_usd"] = selected_sku["target_net_proceeds_usd"]
        if selected_sku.get("site_pricing"):
            payload["sites_to_sell"] = selected_sku["site_pricing"]
    profit_guidance = profit_methodology(edited["pricing_plan"])
    edited["pricing_plan"]["profit_guidance"] = profit_guidance
    edited["pricing_plan"]["status"] = profit_guidance["status"]
    edited["pricing_plan"]["note"] = (
        "已按卖家净回款和当前成本生成贡献利润估算；估算值不作为审核硬门槛。"
        if profit_guidance.get("recommended_net_proceeds_range_usd")
        else "核心成本仍有缺项，暂未生成利润区间；该状态不阻止人工审核。"
    )

    gallery = changes.get("gallery")
    if isinstance(gallery, list):
        image_review = ((source.get("evidence") or {}).get("image_review") or {})
        original_gallery = image_review.get("gallery") or []
        source_gallery = image_review.get("source_gallery") or original_gallery
        allowed = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in source_gallery if isinstance(item, dict)}
        allowed_remote_urls = {
            str(item.get("remote_url")) for item in [*source_gallery, *original_gallery]
            if isinstance(item, dict) and item.get("remote_url")
        }
        allowed_local_files = {
            str(item.get("local_file")) for item in [*source_gallery, *original_gallery]
            if isinstance(item, dict) and item.get("local_file")
        }
        latest_urls: set[str] = set()
        source_id = str((source.get("evidence") or {}).get("source_product_id") or "")
        if source_id.isdigit():
            captures = sorted((source_path.parents[0].parent / "data" / "intake").glob(f"1688-{source_id}-*.json"))
            if captures:
                latest = read_json(captures[-1])
                for url in latest.get("product_images") or latest.get("images") or []:
                    latest_urls.add(str(url))
                    allowed.add(json.dumps({"role": "latest", "remote_url": url, "image_type": "product", "review_status": "UNREVIEWED", "source": "latest_capture"}, ensure_ascii=False, sort_keys=True))
                for row in latest.get("sku_images") or []:
                    if isinstance(row, dict) and row.get("url"):
                        latest_urls.add(str(row["url"]))
                        allowed.add(json.dumps({"role": "latest_sku", "remote_url": row["url"], "image_type": "sku", "variant": row.get("variant") or "", "sku_id": row.get("sku_id") or "", "review_status": "UNREVIEWED", "source": "latest_capture"}, ensure_ascii=False, sort_keys=True))
        safe_gallery = []
        for item in gallery:
            if not isinstance(item, dict):
                continue
            serialized = json.dumps(item, ensure_ascii=False, sort_keys=True)
            remote_url = str(item.get("remote_url") or "")
            linked = item.get("source") == "linked" and urlparse(remote_url).scheme == "https"
            latest_capture = item.get("source") == "latest_capture" and remote_url in latest_urls
            local_file = str(item.get("local_file") or "")
            assets_root = (source_path.parents[0].parent / "assets").resolve()
            local_path = (assets_root / local_file).resolve() if local_file else None
            local_exists = bool(
                local_path
                and local_path.is_relative_to(assets_root)
                and local_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                and local_path.is_file()
            )
            known_source = remote_url in allowed_remote_urls or local_file in allowed_local_files
            if serialized in allowed or known_source or linked or latest_capture or local_exists:
                safe_gallery.append(item)
            if len(safe_gallery) >= 20:
                break
        evidence.setdefault("image_review", {})["gallery"] = safe_gallery
        evidence["image_review"]["source_gallery"] = copy.deepcopy(source_gallery)

    locks = changes.get("field_locks") if isinstance(changes.get("field_locks"), dict) else {}
    edited["status"] = "DRAFT_LOCAL_ONLY"
    edited["requested_action"] = "draft"
    edited["human_approved"] = False
    prior_metadata = source.get("edit_metadata") if isinstance(source.get("edit_metadata"), dict) else {}
    edited["edit_metadata"] = {
        "base_draft": source_path.name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "field_locks": {key: value is True for key, value in locks.items()},
        "attribute_sources": attribute_sources,
        "publishing_performed": False,
    }
    if prior_metadata.get("manual_review_requested_at"):
        edited["edit_metadata"]["manual_review_requested_at"] = prior_metadata["manual_review_requested_at"]
        edited["edit_metadata"]["manual_review_source"] = prior_metadata.get("manual_review_source") or "dashboard_batch"

    errors, warnings = validate(edited)
    edited["local_validation"] = {"errors": errors, "warnings": warnings}
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = versions_dir / f"{source_path.stem}-{stamp}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def save_edited_draft(
    name: str,
    changes: dict[str, Any],
    drafts_dir: Path = DRAFTS_DIR,
    versions_dir: Path = VERSIONS_DIR,
) -> tuple[Path, Path, Path]:
    """Persist editor choices to the working draft while preserving history."""
    source_path = _draft_path(name, drafts_dir)
    edited_snapshot = create_edited_version(name, changes, drafts_dir, versions_dir)
    edited = read_json(edited_snapshot)
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = versions_dir / f"{source_path.stem}-before-save-{stamp}.json"
    backup.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = source_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(source_path)
    return source_path, edited_snapshot, backup


def make_handler(root: Path = ROOT):
    drafts_dir = root / "drafts"
    versions_dir = root / "draft_versions"
    web_dir = root / "web"
    assets_dir = root / "assets"

    class Handler(BaseHTTPRequestHandler):
        server_version = "MeikeduoDraftEditor/0.1"

        def _json(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self.path.startswith("/api/collect") and self.headers.get("Origin") == YOUYOU_EXTENSION_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", YOUYOU_EXTENSION_ORIGIN)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                return self._json(404, {"ok": False, "error": "not_found"})
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            if self.path != "/api/collect" or self.headers.get("Origin") != YOUYOU_EXTENSION_ORIGIN:
                return self._json(403, {"ok": False, "error": "extension_not_allowed"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", YOUYOU_EXTENSION_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Youyou-Collector")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/oauth/mercadolibre/callback":
                query = parse_qs(parsed.query)
                code = (query.get("code") or [""])[0]
                state = (query.get("state") or [""])[0]
                ok, message = False, "授权参数不完整"
                if code and state:
                    try:
                        complete_authorization(root, code, state)
                        ok, message = True, "授权成功，店铺令牌已加密保存在本机"
                    except Exception as exc:
                        message = f"授权失败：{str(exc)[:300]}"
                body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>美客多授权</title>"
                        f"<body style='font-family:Microsoft YaHei,sans-serif;padding:40px'><h2>{html.escape(message)}</h2>"
                        "<p>可以关闭此页面，回到店铺管理点击“刷新连接状态”。</p></body></html>").encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path in ("/", "/index.html"):
                return self._file(web_dir / "dashboard.html", "text/html; charset=utf-8")
            if parsed.path in ("/editor", "/editor.html"):
                return self._file(web_dir / "product_editor.html", "text/html; charset=utf-8")
            if parsed.path in ("/review", "/review.html"):
                return self._file(web_dir / "review.html", "text/html; charset=utf-8")
            if parsed.path in ("/publish", "/publish.html"):
                return self._file(web_dir / "publish.html", "text/html; charset=utf-8")
            if parsed.path in ("/stores", "/stores.html"):
                return self._file(web_dir / "stores.html", "text/html; charset=utf-8")
            if parsed.path in ("/ai-settings", "/ai-settings.html"):
                return self._file(web_dir / "ai_settings.html", "text/html; charset=utf-8")
            if parsed.path == "/dashboard.js":
                return self._file(web_dir / "dashboard.js", "text/javascript; charset=utf-8")
            if parsed.path == "/dashboard.css":
                return self._file(web_dir / "dashboard.css", "text/css; charset=utf-8")
            if parsed.path == "/editor.js":
                return self._file(web_dir / "editor.js", "text/javascript; charset=utf-8")
            if parsed.path == "/editor.css":
                return self._file(web_dir / "editor.css", "text/css; charset=utf-8")
            if parsed.path == "/review.js":
                return self._file(web_dir / "review.js", "text/javascript; charset=utf-8")
            if parsed.path == "/review.css":
                return self._file(web_dir / "review.css", "text/css; charset=utf-8")
            if parsed.path == "/publish.js":
                return self._file(web_dir / "publish.js", "text/javascript; charset=utf-8")
            if parsed.path == "/publish.css":
                return self._file(web_dir / "publish.css", "text/css; charset=utf-8")
            if parsed.path == "/stores.js":
                return self._file(web_dir / "stores.js", "text/javascript; charset=utf-8")
            if parsed.path == "/stores.css":
                return self._file(web_dir / "stores.css", "text/css; charset=utf-8")
            if parsed.path == "/ai-settings.js":
                return self._file(web_dir / "ai_settings.js", "text/javascript; charset=utf-8")
            if parsed.path == "/api/health":
                return self._json(200, {"ok": True, "mode": "LOCAL_DRAFT_ONLY"})
            if parsed.path == "/api/model-settings":
                return self._json(200, {"ok": True, **get_public_settings(root)})
            if parsed.path == "/api/store-status":
                live = parse_qs(parsed.query).get("live", ["0"])[0] == "1"
                return self._json(200, {"ok": True, "store": store_status(root, live=live)})
            if parsed.path == "/api/image-proxy":
                try:
                    url = (parse_qs(parsed.query).get("url") or [""])[0]
                    image_path, content_type = cache_remote_image(url, assets_dir / "remote-cache")
                    return self._file(image_path, content_type)
                except (EditorError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/drafts":
                return self._json(200, {"ok": True, "items": list_drafts(drafts_dir, root)})
            if parsed.path == "/api/category-tree":
                try:
                    query = parse_qs(parsed.query)
                    site_id = (query.get("site_id") or ["CBT"])[0]
                    parent_id = (query.get("parent_id") or [""])[0]
                    phrase = (query.get("q") or [""])[0]
                    return self._json(200, {"ok": True, **browse_official_categories(site_id, parent_id, phrase)})
                except (EditorError, MercadoLibreAPIError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            log_match = re.fullmatch(r"/api/drafts/([^/]+)/ai-log", parsed.path)
            if log_match:
                try:
                    return self._json(200, {"ok": True, "events": draft_ai_log(log_match.group(1), drafts_dir, root)})
                except (EditorError, json.JSONDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            preview_match = re.fullmatch(r"/api/drafts/([^/]+)/publish-preview", parsed.path)
            if preview_match:
                try:
                    draft = read_json(_draft_path(preview_match.group(1), drafts_dir))
                    return self._json(200, {"ok": True, "preview": build_publish_preview(draft)})
                except (EditorError, PublishError, json.JSONDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path.startswith("/api/categories/"):
                try:
                    category_id = parsed.path.removeprefix("/api/categories/")
                    return self._json(200, {"ok": True, "category": query_official_category(category_id, root)})
                except (EditorError, json.JSONDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path.startswith("/api/drafts/"):
                try:
                    name = parsed.path.removeprefix("/api/drafts/")
                    return self._json(200, {"ok": True, **get_draft(name, drafts_dir, root)})
                except (EditorError, json.JSONDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path.startswith("/assets/"):
                relative = Path(unquote(parsed.path.removeprefix("/assets/")))
                candidate = (assets_dir / relative).resolve()
                if assets_dir.resolve() not in candidate.parents:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                content_type = "image/png" if candidate.suffix.lower() == ".png" else "image/jpeg"
                return self._file(candidate, content_type)
            return self._json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            origin = self.headers.get("Origin")
            local_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            allowed_origin = {YOUYOU_EXTENSION_ORIGIN} if parsed.path == "/api/collect" else local_origins
            if origin and origin not in allowed_origin:
                return self._json(403, {"ok": False, "error": "request_origin_not_allowed"})
            if parsed.path == "/api/publish/batch":
                try:
                    origin = str(self.headers.get("Origin") or "")
                    if origin and not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d+", origin):
                        return self._json(403, {"ok": False, "error": "发布请求来源不正确"})
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 100_000:
                        raise PublishError("批量发布确认内容大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    if value.get("confirmed") is not True:
                        raise PublishError("批量真实发布需要再次确认")
                    names = value.get("names") if isinstance(value.get("names"), list) else []
                    if not names or len(names) > 50:
                        raise PublishError("请选择 1 至 50 个商品")
                    results = []
                    for name in names:
                        try:
                            record = publish_draft(str(name), confirmed=True, drafts_dir=drafts_dir, root=root,
                                                   preflight_issues=publish_preflight_issues)
                            results.append({"name": name, "ok": True, "status": record.get("status")})
                        except (PublishError, MercadoLibreAPIError, OSError) as exc:
                            results.append({"name": name, "ok": False, "error": str(exc)})
                    return self._json(200, {"ok": True, "results": results})
                except (PublishError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/drafts/batch":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 100_000:
                        raise EditorError("批量操作内容大小不正确。")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = batch_draft_action(str(value.get("action") or ""), value.get("names") if isinstance(value.get("names"), list) else [], drafts_dir, root)
                    return self._json(200, {"ok": True, **result})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/model-settings":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 100_000:
                        raise ModelSettingsError("AI 配置内容大小不正确。")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    return self._json(200, {"ok": True, **save_model_settings(root, value)})
                except (ModelSettingsError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/store-preferences":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    result = save_store_preferences(root, str(value.get("alias") or ""), str(value.get("auth_type") or "CBT"))
                    return self._json(200, {"ok": True, "store": result})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/store-active":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    return self._json(200, {"ok": True, "store": set_active_store(root, str(value.get("store_id") or ""))})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/store-oauth-config":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 20_000:
                        raise ValueError("OAuth 配置内容大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    return self._json(200, {"ok": True, "oauth_config": save_oauth_configuration(root, value)})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/store-authorization-url":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    result = authorization_url(
                        root,
                        str(value.get("alias") or ""),
                        str(value.get("auth_type") or "CBT"),
                        str(value.get("store_id") or ""),
                    )
                    return self._json(200, {"ok": True, **result})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            model_test = re.fullmatch(r"/api/model-settings/(vision|images|attributes)/test", parsed.path)
            if model_test:
                try:
                    return self._json(200, {"ok": True, **test_connection(root, model_test.group(1))})
                except (ModelSettingsError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/upload-image":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 11_000_000:
                        raise EditorError("上传图片大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    local_file = save_uploaded_image(str(value.get("name") or "image"), str(value.get("data_url") or ""), assets_dir)
                    return self._json(201, {"ok": True, "local_file": local_file})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if parsed.path == "/api/collect":
                if self.headers.get("Origin") != YOUYOU_EXTENSION_ORIGIN or self.headers.get("X-Youyou-Collector") != "1":
                    return self._json(403, {"ok": False, "error": "只接受悠悠采集扩展"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 2_000_000:
                        raise IntakeError("采集数据大小不正确")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = save_collection_and_enqueue(payload, root)
                    return self._json(201, {
                        "ok": True,
                        "draft_created": result["draft_created"],
                        "status": result["queue_status"],
                        "validation_stage": result["validation_stage"],
                        "next_action": result["next_action"],
                        "image_counts": result["image_counts"],
                        "package_ai_status": result["package_ai_status"],
                    })
                except (IntakeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            match = re.fullmatch(r"/api/drafts/([^/]+)/save-version", parsed.path)
            full_flow_match = re.fullmatch(r"/api/drafts/([^/]+)/full-ai-flow", parsed.path)
            category_discovery_match = re.fullmatch(r"/api/drafts/([^/]+)/discover-category", parsed.path)
            competitor_match = re.fullmatch(r"/api/drafts/([^/]+)/competitor-pricing", parsed.path)
            attribute_fill_match = re.fullmatch(r"/api/drafts/([^/]+)/complete-attributes", parsed.path)
            image_match = re.fullmatch(r"/api/drafts/([^/]+)/generate-images", parsed.path)
            sku_image_match = re.fullmatch(r"/api/drafts/([^/]+)/generate-sku-images", parsed.path)
            prompt_match = re.fullmatch(r"/api/drafts/([^/]+)/image-prompts", parsed.path)
            publish_match = re.fullmatch(r"/api/drafts/([^/]+)/publish", parsed.path)
            retry_match = re.fullmatch(r"/api/drafts/([^/]+)/publish-retry", parsed.path)
            refresh_match = re.fullmatch(r"/api/drafts/([^/]+)/publish-refresh", parsed.path)
            if retry_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    record = publish_draft(unquote(retry_match.group(1)), confirmed=value.get("confirmed") is True,
                                           drafts_dir=drafts_dir, root=root, preflight_issues=publish_preflight_issues,
                                           retry_failed_only=True)
                    return self._json(200, {"ok": True, "publish": record})
                except (PublishError, EditorError, MercadoLibreAPIError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if refresh_match:
                try:
                    record = refresh_publish_record(unquote(refresh_match.group(1)), drafts_dir=drafts_dir, root=root)
                    return self._json(200, {"ok": True, "publish": record})
                except (PublishError, MercadoLibreAPIError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if publish_match:
                try:
                    origin = str(self.headers.get("Origin") or "")
                    if origin and not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d+", origin):
                        return self._json(403, {"ok": False, "error": "发布请求来源不正确"})
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 10_000:
                        raise PublishError("发布确认内容大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    record = publish_draft(
                        unquote(publish_match.group(1)),
                        confirmed=value.get("confirmed") is True,
                        drafts_dir=drafts_dir,
                        root=root,
                        preflight_issues=publish_preflight_issues,
                    )
                    return self._json(201, {"ok": True, "publish": record})
                except (PublishError, EditorError, MercadoLibreAPIError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if full_flow_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    if value.get("confirmed") is not True:
                        raise EditorError("完整 AI 流程会调用文案、属性和生图 API，请确认预计图片数量后再提交。")
                    job = start_full_ai_flow(full_flow_match.group(1), drafts_dir, root)
                    return self._json(202, {"ok": True, "job": job, "publishing_performed": False})
                except (EditorError, ModelSettingsError, AIListingError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if category_discovery_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    result = discover_official_category(str(value.get("title") or ""), str(value.get("site_id") or "CBT"), str(value.get("category_id") or ""), root)
                    return self._json(200, {"ok": True, **result})
                except (EditorError, MercadoLibreAPIError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if competitor_match:
                try:
                    path = _draft_path(competitor_match.group(1), drafts_dir)
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    draft = read_json(path)
                    if isinstance(value.get("payload"), dict):
                        draft.setdefault("payload", {}).update(value["payload"])
                    if isinstance(value.get("pricing"), dict):
                        draft.setdefault("pricing_plan", {}).update(value["pricing"])
                    result = collect_competitor_pricing(draft)
                    return self._json(200, {"ok": True, "analysis": result})
                except (EditorError, MercadoLibreAPIError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if attribute_fill_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    if value.get("confirmed") is not True:
                        raise EditorError("AI 回填属性会消耗文本模型额度，请先确认。")
                    result = suggest_required_attributes(attribute_fill_match.group(1), str(value.get("site_id") or "CBT"), str(value.get("title") or ""), str(value.get("category_id") or ""), value.get("attributes") if isinstance(value.get("attributes"), list) else [], drafts_dir, root)
                    return self._json(200, {"ok": True, **result})
                except (EditorError, ModelSettingsError, AIListingError, MercadoLibreAPIError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if prompt_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    draft = read_json(_draft_path(prompt_match.group(1), drafts_dir))
                    language = resolve_market_language(draft, str(value.get("language") or "auto"))
                    return self._json(200, {"ok": True, "prompts": build_image_prompts(draft, language), "resolved_language": language})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if image_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 100_000:
                        raise EditorError("生图请求大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    job = start_image_generation(
                        image_match.group(1),
                        str(value.get("language") or "none"),
                        value.get("prompts") if isinstance(value.get("prompts"), list) else None,
                        drafts_dir,
                        root,
                    )
                    return self._json(202, {"ok": True, "job": job, "publishing_performed": False})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if sku_image_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    if value.get("confirmed") is not True:
                        raise EditorError("生成每个 SKU 的专属图会消耗额度，请在页面确认数量后再提交。")
                    job = start_sku_image_generation(sku_image_match.group(1), drafts_dir, root)
                    return self._json(202, {"ok": True, "job": job, "publishing_performed": False})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            approve_match = re.fullmatch(r"/api/drafts/([^/]+)/approve-review", parsed.path)
            review_images_match = re.fullmatch(r"/api/drafts/([^/]+)/review-images", parsed.path)
            review_sku_images_match = re.fullmatch(r"/api/drafts/([^/]+)/review-sku-images", parsed.path)
            if review_sku_images_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 2_000_000:
                        raise EditorError("SKU 图片调整内容大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = update_review_sku_galleries(
                        review_sku_images_match.group(1), value.get("sku_galleries"), drafts_dir, versions_dir, root
                    )
                    return self._json(200, {"ok": True, **result})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if review_images_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 500_000:
                        raise EditorError("图片调整内容大小不正确")
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = update_review_gallery(
                        review_images_match.group(1), value.get("gallery"), drafts_dir, versions_dir, root
                    )
                    return self._json(200, {"ok": True, **result})
                except (EditorError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if approve_match:
                try:
                    record = approve_local_review(approve_match.group(1), drafts_dir, root)
                    return self._json(201, {"ok": True, "review": record})
                except (EditorError, json.JSONDecodeError) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            if not match:
                return self._json(404, {"ok": False, "error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 500_000:
                    raise EditorError("编辑内容大小不正确")
                changes = json.loads(self.rfile.read(length).decode("utf-8"))
                destination, snapshot, backup = save_edited_draft(
                    match.group(1), changes, drafts_dir, versions_dir
                )
                saved = read_json(destination)
                return self._json(
                    201,
                    {
                        "ok": True,
                        "saved": str(destination),
                        "snapshot": str(snapshot),
                        "backup": str(backup),
                        "validation": saved["local_validation"],
                        "working_draft_updated": True,
                        "publishing_performed": False,
                    },
                )
            except (EditorError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="美客多本机商品详情编辑页")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler())
    print(
        json.dumps(
            {
                "ok": True,
                "url": f"http://127.0.0.1:{args.port}",
                "mode": "LOCAL_DRAFT_ONLY",
                "publishing_performed": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
